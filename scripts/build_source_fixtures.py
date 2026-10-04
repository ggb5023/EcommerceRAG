#!/usr/bin/env python3
"""Build deterministic parser/chunking fixtures outside the repository."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")
GENERATED_AT = "2026-10-03"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_docx(path: Path, paragraphs: list[tuple[str, str]], tables: list[list[list[str]]]) -> None:
    body = []
    for style, text in paragraphs:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    for table in tables:
        rows = []
        for row in table:
            cells = "".join(
                f'<w:tc><w:p><w:r><w:t xml:space="preserve">{escape(value)}</w:t></w:r></w:p></w:tc>'
                for value in row
            )
            rows.append(f"<w:tr>{cells}</w:tr>")
        body.append(f"<w:tbl>{''.join(rows)}</w:tbl>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>")
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)


def write_text(path: Path, value: str, *, encoding: str = "utf-8", bom: bool = False) -> None:
    data = value.encode(encoding)
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def build(root: Path) -> dict[str, object]:
    if root.exists():
        shutil.rmtree(root)
    for name in ("markdown", "csv", "docx", "pdf", "invalid", "reports"):
        (root / name).mkdir(parents=True)

    markdown = {
        "md-structure-v1.md": """---\nfixture_id: md-structure-v1\nscenario: heading_jump_table_code_image\n---\n# 合成商品资料夹具\n\n这是只用于解析和切片的合成资料，不代表任何商家政策。\n\n### 跳级标题\n\n正文用于验证标题栈恢复。\n\n```python\n# 代码块中的井号不是标题\nprint('fixture')\n```\n\n| 字段 | 示例 |\n| --- | --- |\n| sku | SYN-001 |\n| 状态 | fixture-only |\n\n![缺失图片](images/missing.png)\n\n## 第二章节\n\n表格前后正文不能被错误拼接。""",
        "md-faq-short-v1.md": """# 合成 FAQ\n\n## 配送问题\n\n问：测试商品何时发货？\n\n答：这是合成回答，只用于短问答和引用定位测试。\n\n## 规格问题\n\n问：两个型号有什么区别？\n\n答：只依据同一文档中的规格表，不推断未提供属性。""",
        "md-policy-long-v1.md": """# 合成政策长文\n\n## 退货条款\n\n本节使用多段中文长文本验证按句子、段落和硬上限切片。日期、角色和金额都是测试值，不能作为真实业务依据。系统应保留标题路径、版本、租户和披露分类，并在超预算时记录 split_reason。\n\n### 例外\n\n表格应保持独立元素；表格前后正文不能仅凭相同标题跨表格连接。""",
    }
    for name, content in markdown.items():
        write_text(root / "markdown" / name, content)

    write_text(root / "csv" / "csv-products-record-v1.csv", "sku_id,name,price,tenant_id,shop_id\nSYN-001,合成传感器,199,demo-tenant-a,demo-shop-east\nSYN-002,合成扩展板,299,demo-tenant-a,demo-shop-east\n")
    write_text(root / "csv" / "csv-quoted-multiline-v1.csv", 'sku_id,description\nSYN-003,"第一行\n第二行，含逗号, 仍是一个字段"\n')
    write_text(root / "csv" / "csv-long-cell-v1.csv", "sku_id,description\nSYN-LONG," + "长字段切片测试。" * 120 + "\n")
    write_text(root / "csv" / "csv-utf8-bom-v1.csv", "sku_id,name\nSYN-BOM,UTF-8 BOM 测试\n", bom=True)
    write_text(root / "csv" / "csv-utf16-v1.csv", "sku_id,name\nSYN-UTF16,需显式编码配置\n", encoding="utf-16")
    write_text(root / "csv" / "csv-bad-rows-v1.csv", "sku_id,name,price\nSYN-BAD,列数正确,9\nSYN-BAD-2,列数缺失\n")

    write_docx(root / "docx" / "docx-heading-table-v1.docx", [
        ("Title", "合成 Word 解析夹具"), ("Heading1", "规格章节"),
        ("", "普通段落用于验证标题路径和正文顺序。"), ("Heading2", "接口表"),
        ("", "表格与正文混排，内容仅用于工程测试。"),
    ], [[["字段", "值"], ["接口", "SYN-I2C"], ["状态", "fixture-only"]]])
    write_docx(root / "docx" / "docx-repeated-header-v1.docx", [("Heading1", "跨页表格模拟")],
               [[["型号", "说明"], ["SYN-A", "第一段"], ["型号", "说明"], ["SYN-B", "第二段"]]])
    write_docx(root / "docx" / "docx-long-paragraph-v1.docx", [("Heading1", "长段落"), ("", "长段落切片测试。" * 260)], [])

    (root / "invalid" / "broken-docx.docx").write_bytes(b"not-a-docx")
    (root / "invalid" / "empty.bin").write_bytes(b"")
    (root / "invalid" / "invalid-utf8.csv").write_bytes(b"sku_id,name\nSYN-BAD,\xff\xfe\n")
    write_text(root / "invalid" / "path-traversal.manifest.yaml", "path: ../../etc/passwd\nfixture_id: invalid-path\n")
    write_text(root / "invalid" / "unsupported.html", "<html><body>unsupported fixture</body></html>\n")

    tags = {
        "md-structure-v1": ["heading_jump", "code_fence", "table", "missing_image"],
        "md-faq-short-v1": ["short_faq", "question_answer"],
        "md-policy-long-v1": ["long_text", "section_boundary", "hard_split"],
        "csv-products-record-v1": ["record_mode", "tenant_filter", "row_boundary"],
        "csv-quoted-multiline-v1": ["quoted_multiline", "csv_reader"],
        "csv-long-cell-v1": ["long_cell", "hard_split"],
        "csv-utf8-bom-v1": ["encoding_bom", "encoding_review"],
        "csv-utf16-v1": ["encoding_utf16", "explicit_config"],
        "csv-bad-rows-v1": ["column_mismatch", "validation_error"],
        "docx-heading-table-v1": ["heading", "table", "mixed_content"],
        "docx-repeated-header-v1": ["repeated_header", "table_boundary"],
        "docx-long-paragraph-v1": ["long_text", "hard_split"],
        "broken-docx": ["corrupt_container", "failure_path"], "empty": ["empty_file", "preflight_failure"],
        "invalid-utf8": ["encoding_failure", "preflight_failure"], "path-traversal": ["manifest_path_traversal", "security_failure"],
        "unsupported": ["unsupported_type", "preflight_failure"],
    }
    entries = []
    for directory in ("markdown", "csv", "docx", "invalid"):
        for path in sorted((root / directory).iterdir()):
            fixture_id = path.stem.replace(".manifest", "")
            suffix = path.suffix.lower()
            fmt = {".md": "markdown", ".csv": "csv", ".docx": "docx"}.get(suffix, "invalid")
            entries.append({"fixture_id": fixture_id, "format": fmt, "path": str(path.relative_to(root)),
                            "scenario_tags": tags[fixture_id], "source_type": "synthetic_engineering_fixture",
                            "generated_at": GENERATED_AT, "license_status": "internal-generated",
                            "expected_parse_status": "expected_failure"
                            if directory == "invalid" or fixture_id in {"csv-utf16-v1", "csv-bad-rows-v1"}
                            else "parseable",
                            "size_bytes": path.stat().st_size, "sha256": digest(path),
                            "allowed_use": ["parser regression", "chunking experiment", "citation position mapping"],
                            "forbidden_use": ["merchant policy truth", "price/inventory truth", "tenant authorization truth", "customer reply acceptance"],
                            "real_service_acceptance": False})
    (root / "pdf" / "README.txt").write_text("PDF fixtures are referenced from ../real-docs-v1/raw and are not duplicated.\n", encoding="utf-8")
    manifest = {"manifest_version": "source-fixtures-v1", "generated_at": GENERATED_AT,
                "real_pdf_track_manifest": "/var/lib/ecommerce-rag/real-docs/real-docs-v1/manifest.json",
                "storage_policy": "Restricted outside Git; raw inputs and reports are not copied into repository artifacts.",
                "real_service_acceptance": False, "fixtures": entries}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(root, 0o700)
    for directory in root.iterdir():
        if directory.is_dir():
            os.chmod(directory, 0o700)
            for path in directory.iterdir():
                os.chmod(path, 0o600)
    os.chmod(root / "manifest.json", 0o600)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    manifest = build(args.root)
    print(json.dumps({"root": str(args.root), "fixtures": len(manifest["fixtures"]), "generated_at": GENERATED_AT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
