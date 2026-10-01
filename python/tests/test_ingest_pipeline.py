from pathlib import Path
from dataclasses import replace
from zipfile import ZipFile

from app.ingest.pipeline import CSVParser, DOCXParser, MarkdownParser, LocalIndex, chunk_elements


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


def test_adjacent_expansion_reapplies_all_scope_filters(tmp_path: Path):
    path = tmp_path / "long.md"
    path.write_text("目标商品" + "邻接正文" * 30, encoding="utf-8")
    chunks = chunk_elements(MarkdownParser().parse(path, document_id="doc", version_id="v1",
                                                   metadata=metadata()), max_chars=20)
    hit, neighbor = chunks[:2]
    forbidden = [replace(neighbor, chunk_id="other-tenant", tenant_id="tenant-b"),
                 replace(neighbor, chunk_id="other-shop", shop_id="shop-b"),
                 replace(neighbor, chunk_id="other-document", document_id="restricted"),
                 replace(neighbor, chunk_id="old-version", version_id="v0"),
                 replace(neighbor, chunk_id="other-section", section_seq=2),
                 replace(neighbor, chunk_id="internal", disclosure_class="internal_only"),
                 replace(neighbor, chunk_id="expired", effective_to="2026-02-01")]
    index = LocalIndex([hit, neighbor, *forbidden])
    scope = dict(tenant_id="tenant-a", shop_id="shop-a", allowed_shop_ids={"shop-a"},
                 allowed_document_ids={"doc"}, role="operator", business_date="2026-10-02")
    assert {r["chunk_id"] for r in index.search("目标商品", **scope)} == {hit.chunk_id}
    assert {r["chunk_id"] for r in index.search("目标商品", adjacent_window=1, **scope)} == {hit.chunk_id, neighbor.chunk_id}
    scope["allowed_document_ids"] = set()
    assert not index.search("目标商品", adjacent_window=1, **scope)
    scope["allowed_document_ids"] = {"doc"}
    index.activate("tenant-a", "doc", "v0")
    assert not index.search("目标商品", adjacent_window=1, **scope)
