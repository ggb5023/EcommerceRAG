from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

from app.ingest.pipeline import (
    CSVParser,
    DOCXParser,
    HTMLParserAdapter,
    LocalIndex,
    MarkdownParser,
    ParsedElement,
    chunk_elements,
    chunk_elements_v2,
    validate_chunk_document_versions,
)


def metadata():
    return {
        "tenant_id": "tenant-a", "shop_id": "shop-a", "disclosure_class": "external_allowed",
        "effective_from": "2026-01-01", "effective_to": None,
    }


def test_markdown_headings_and_long_text(tmp_path: Path):
    path = tmp_path / "guide.md"
    path.write_text("# 商品\n\n## 护理\n\n中文说明。" + "很长。" * 300, encoding="utf-8")
    elements = MarkdownParser().parse(path, document_id="doc", version_id="v1", metadata=metadata())
    assert any(element.heading == ("商品", "护理") for element in elements)
    chunks = chunk_elements(elements, max_chars=100)
    assert len(chunks) > 2
    assert any(chunk.split_reason == "hard_split" for chunk in chunks)
    assert all(chunk.chunk_hash and chunk.section_seq for chunk in chunks)


def test_docx_headings_paragraphs_and_table(tmp_path: Path):
    path = tmp_path / "guide.docx"
    xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>商品规格</w:t></w:r></w:p>
    <w:p><w:r><w:t>中文规格内容</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>型号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>容量</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>480ml</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>'''
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    elements = DOCXParser().parse(path, document_id="doc", version_id="v1", metadata=metadata())
    assert any("中文" in element.content for element in elements)
    assert any(element.element_type == "table" and "480ml" in element.content for element in elements)


def test_docx_fallback_preserves_block_order_heading_path_and_repeated_header_warning(tmp_path: Path):
    path = tmp_path / "ordered.docx"
    xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>规格</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>型号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>值</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>型号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>值</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:t>表格之后的说明。</w:t></w:r></w:p>
    </w:body></w:document>'''
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    elements = DOCXParser().parse(path, document_id="doc", version_id="v1", metadata=metadata())
    assert [element.element_type for element in elements] == ["heading", "table", "text"]
    assert elements[1].heading == ("规格",)
    assert elements[1].warning == "repeated_table_header"
    assert elements[0].source_position["block_index"] < elements[1].source_position["block_index"]
    assert elements[1].source_position["block_index"] < elements[2].source_position["block_index"]


def test_csv_shared_file_filters_rows_to_manifest_scope(tmp_path: Path):
    path = tmp_path / "products.csv"
    path.write_text(
        "tenant_id,shop_id,sku\n"
        "tenant-a,shop-a,A\n"
        "tenant-b,shop-b,B\n",
        encoding="utf-8",
    )
    elements = CSVParser().parse(path, document_id="products-a", version_id="v1", metadata=metadata())
    assert len(elements) == 1
    assert "sku: A" in elements[0].content


def test_csv_bom_keeps_tenant_filtering_at_header_boundary(tmp_path: Path):
    path = tmp_path / "products-bom.csv"
    path.write_bytes(
        b"\xef\xbb\xbftenant_id,shop_id,sku\n"
        b"tenant-b,shop-a,OTHER\n"
        b"tenant-a,shop-a,OWN\n"
    )
    elements = CSVParser().parse(path, document_id="products-a", version_id="v1", metadata=metadata())
    assert len(elements) == 1
    assert "sku: OWN" in elements[0].content
    assert all("OTHER" not in element.content for element in elements)


def test_csv_quoted_multiline_records_keep_physical_source_range(tmp_path: Path):
    path = tmp_path / "multiline.csv"
    path.write_text(
        "sku_id,description\n"
        "SYN-003,\"第一行\n第二行，含逗号, 仍是一个字段\"\n",
        encoding="utf-8",
    )
    elements = CSVParser().parse(path, document_id="products", version_id="v1", metadata=metadata())
    assert len(elements) == 1
    assert elements[0].source_position == {"line_start": 2, "line_end": 3, "record_number": 1}
    assert "第二行，含逗号" in elements[0].content


def test_csv_rejects_duplicate_or_empty_headers(tmp_path: Path):
    import pytest

    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text("sku_id,sku_id\nSYN-001,value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="csv_header_duplicate"):
        CSVParser().parse(duplicate, document_id="doc", version_id="v1", metadata=metadata())

    empty = tmp_path / "empty-header.csv"
    empty.write_text("sku_id,\nSYN-001,value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="csv_header_empty"):
        CSVParser().parse(empty, document_id="doc", version_id="v1", metadata=metadata())


def test_csv_rejects_column_mismatch_with_record_location(tmp_path: Path):
    import pytest

    path = tmp_path / "bad-rows.csv"
    path.write_text("sku_id,name,price\nSYN-001,完整,9\nSYN-002,缺列\n", encoding="utf-8")
    with pytest.raises(ValueError, match="csv_column_mismatch:row=3"):
        CSVParser().parse(path, document_id="doc", version_id="v1", metadata=metadata())


def test_adjacent_expansion_reapplies_all_scope_filters(tmp_path: Path):
    path = tmp_path / "long.md"
    path.write_text("目标商品" + "邻接正文" * 30, encoding="utf-8")
    chunks = chunk_elements(MarkdownParser().parse(path, document_id="doc", version_id="v1",
                                                   metadata=metadata()), max_chars=20)
    hit, neighbor = chunks[:2]
    forbidden = [replace(neighbor, chunk_id="other-tenant", tenant_id="tenant-b"),
                 replace(neighbor, chunk_id="other-shop", document_id="other-shop-doc", shop_id="shop-b"),
                 replace(neighbor, chunk_id="other-document", document_id="restricted"),
                 replace(neighbor, chunk_id="old-version", version_id="v0"),
                 replace(neighbor, chunk_id="other-section", section_seq=2),
                 replace(neighbor, chunk_id="internal", disclosure_class="internal_only"),
                 replace(neighbor, chunk_id="expired", effective_to="2026-02-01")]
    index = LocalIndex([hit, neighbor, *forbidden])
    scope = {
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "allowed_shop_ids": {"shop-a"},
        "allowed_document_ids": {"doc"},
        "role": "operator",
        "business_date": "2026-10-02",
    }
    assert {r["chunk_id"] for r in index.search("目标商品", **scope)} == {hit.chunk_id}
    assert {r["chunk_id"] for r in index.search("目标商品", adjacent_window=1, **scope)} == {hit.chunk_id, neighbor.chunk_id}
    scope["allowed_document_ids"] = set()
    assert not index.search("目标商品", adjacent_window=1, **scope)
    scope["allowed_document_ids"] = {"doc"}
    index.activate("tenant-a", "doc", "v0")
    assert not index.search("目标商品", adjacent_window=1, **scope)


def test_document_version_binding_rejects_duplicate_chunk_ids(tmp_path: Path):
    import pytest

    path = tmp_path / "versioned.md"
    path.write_text("商品规格说明", encoding="utf-8")
    chunks = chunk_elements_v2(MarkdownParser().parse(
        path, document_id="doc", version_id="v1", metadata=metadata()))
    assert chunks
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        validate_chunk_document_versions([chunks[0], replace(chunks[0], content="篡改后的正文")])
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        LocalIndex(chunks).add([chunks[0]])


def test_document_version_binding_rejects_shop_drift_within_version(tmp_path: Path):
    import pytest

    path = tmp_path / "scoped.md"
    path.write_text("同一版本的范围", encoding="utf-8")
    chunks = chunk_elements_v2(MarkdownParser().parse(
        path, document_id="doc", version_id="v1", metadata=metadata()), max_chars=4)
    assert chunks
    drifted = replace(chunks[0], chunk_id="drifted", shop_id="shop-b")
    with pytest.raises(ValueError, match="document_version_scope_mismatch"):
        LocalIndex([chunks[0], drifted])


def test_document_version_binding_allows_explicit_version_switch(tmp_path: Path):
    path = tmp_path / "switch.md"
    path.write_text("版本一商品", encoding="utf-8")
    first = chunk_elements_v2(MarkdownParser().parse(
        path, document_id="doc", version_id="v1", metadata=metadata()))[0]
    second = replace(first, version_id="v2", chunk_id="chunk-v2", content="版本二商品")
    index = LocalIndex([first, second])
    scope = {"tenant_id": "tenant-a", "shop_id": "shop-a", "allowed_shop_ids": {"shop-a"},
             "allowed_document_ids": {"doc"}, "role": "operator"}
    result = index.search("版本二商品", **scope)
    assert result and result[0]["document_version_id"] == "v1"
    assert result[0]["version_id"] == result[0]["document_version_id"]
    index.activate("tenant-a", "doc", "v2")
    result = index.search("版本二商品", **scope)
    assert result and result[0]["document_version_id"] == "v2"
    assert result[0]["version_id"] == result[0]["document_version_id"]


def test_structured_chunks_preserve_sentence_boundaries_and_rule_version(tmp_path: Path):
    path = tmp_path / "structured.md"
    path.write_text("# 标题\n\n第一句说明。第二句说明！第三句说明。" + "长内容。" * 200, encoding="utf-8")
    elements = MarkdownParser().parse(path, document_id="doc", version_id="v2", metadata=metadata())
    chunks = chunk_elements_v2(elements, max_chars=80)
    assert chunks
    assert all(chunk.rule_version == "structured-v2" for chunk in chunks)
    assert all(chunk.source_position["char_end"] > chunk.source_position["char_start"] for chunk in chunks)
    assert any(chunk.split_reason == "sentence_boundary" for chunk in chunks)
    assert any(chunk.split_reason == "hard_split" for chunk in chunks)
    assert [chunk.chunk_hash for chunk in chunks] == [
        chunk.chunk_hash for chunk in chunk_elements_v2(elements, max_chars=80)
    ]


def test_structured_chunks_use_exact_offsets_for_chinese_and_preserve_type(tmp_path: Path):
    path = tmp_path / "types.md"
    path.write_text(
        "# 标题\n\n- 第一项\n- 第二项\n\n| 字段 | 值 |\n| --- | --- |\n| 型号 | PICO |\n\n![图](images/pico.png)\n\n```python\nprint('ok')\n```\n\n甲句。乙句！丙句？",
        encoding="utf-8",
    )
    elements = MarkdownParser().parse(path, document_id="doc", version_id="v1", metadata=metadata())
    assert {element.element_type for element in elements} >= {"heading", "list", "table", "image", "code", "text"}
    chunks = chunk_elements_v2(elements, max_chars=6)
    for chunk in chunks:
        start = chunk.source_position["char_start"]
        end = chunk.source_position["char_end"]
        source = next(element.content for element in elements
                      if element.element_type == chunk.content_type
                      and chunk.heading == element.heading
                      and chunk.metadata.get("tenant_id") == element.metadata.get("tenant_id"))
        assert source[start:end] == chunk.content
    assert any(chunk.content_type == "table" and chunk.metadata["table_body"] for chunk in chunks)
    assert any(chunk.content_type == "image" and chunk.metadata["image_refs"] for chunk in chunks)


def test_structured_chunks_overlap_is_bounded_and_deterministic():
    element = ParsedElement(
        document_id="doc", document_version_id="v1", title="title", heading=(),
        content="第一句。第二句。第三句。", source_position={"line_start": 1},
        metadata=metadata(), disclosure_class="external_allowed",
        effective_from="2026-01-01", effective_to=None,
    )
    first = chunk_elements_v2([element], max_chars=6, overlap_chars=2)
    second = chunk_elements_v2([element], max_chars=6, overlap_chars=2)
    assert first and [item.chunk_hash for item in first] == [item.chunk_hash for item in second]
    assert all(item.content == element.content[item.source_position["char_start"]:item.source_position["char_end"]]
               for item in first)
    assert all(item.source_position["char_end"] <= len(element.content) for item in first)


def test_empty_elements_do_not_create_chunks():
    empty = ParsedElement(
        document_id="doc", document_version_id="v1", title="title", heading=(), content="",
        source_position={}, metadata=metadata(), disclosure_class="external_allowed",
        effective_from=None, effective_to=None,
    )
    assert chunk_elements_v2([empty]) == []


def test_structured_chunk_options_reject_unsafe_budgets():
    element = ParsedElement(
        document_id="doc", document_version_id="v1", title="title", heading=(),
        content="一段内容。", source_position={}, metadata=metadata(),
        disclosure_class="external_allowed", effective_from=None, effective_to=None,
    )
    import pytest

    with pytest.raises(ValueError, match="max_chars"):
        chunk_elements_v2([element], max_chars=0)
    with pytest.raises(ValueError, match="overlap_chars"):
        chunk_elements_v2([element], max_chars=5, overlap_chars=5)
    with pytest.raises(ValueError, match="overlap_chars"):
        chunk_elements_v2([element], max_chars=5, overlap_chars=-1)


def test_html_snapshot_uses_shared_elements_and_chunks(tmp_path: Path):
    path = tmp_path / "snapshot.html"
    path.write_text(
        "<html><head><script>alert('ignore')</script></head><body>"
        "<h1>保温杯</h1><p>容量 &amp; 材质说明。</p>"
        "<ul><li>支持低温清洗</li></ul>"
        "<table><tr><th>型号</th><th>容量</th></tr><tr><td>PICO</td><td>480ml</td></tr></table>"
        "<img src='images/cup.png' alt='产品图'><pre>Do not execute page text</pre>"
        "</body></html>", encoding="utf-8",
    )
    elements = HTMLParserAdapter().parse(path, document_id="web-doc", version_id="v1", metadata=metadata())
    assert {element.element_type for element in elements} >= {"heading", "text", "list", "table", "image", "code"}
    assert any("容量 & 材质" in element.content for element in elements)
    table = next(element for element in elements if element.element_type == "table")
    assert table.table_body == [["型号", "容量"], ["PICO", "480ml"]]
    assert table.source_position["line_start"] >= 1
    assert not any("alert" in element.content for element in elements)
    chunks = chunk_elements_v2(elements, max_chars=80)
    assert chunks and all(chunk.rule_version == "structured-v2" for chunk in chunks)
    assert any(chunk.content_type == "table" for chunk in chunks)
    assert [chunk.chunk_hash for chunk in chunks] == [
        chunk.chunk_hash for chunk in chunk_elements_v2(elements, max_chars=80)
    ]
