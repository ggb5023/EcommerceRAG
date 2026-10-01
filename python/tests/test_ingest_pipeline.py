from pathlib import Path
from zipfile import ZipFile

from app.ingest.pipeline import DOCXParser, MarkdownParser, chunk_elements


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
