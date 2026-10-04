#!/usr/bin/env python3
"""Convert an extracted MinerU result into restricted parsed/chunk artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from app.ingest.pipeline import ParsedElement, chunk_elements_v2
from app.providers.mineru import normalize_content_list


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_content_list(root: Path) -> tuple[list[Any], str]:
    # MinerU result archives commonly prefix the canonical file with a UUID
    # (for example ``<uuid>_content_list.json``), while some fixtures use the
    # unprefixed name.  Accept exactly one canonical v1 list and never silently
    # choose the v2 list when both are present.
    candidates = sorted(
        path for path in root.rglob("*.json")
        if path.name == "content_list.json"
        or (path.name.endswith("_content_list.json") and not path.name.endswith("_content_list_v2.json"))
    )
    if not candidates:
        raise ValueError("content_list_missing")
    if len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_file():
        raise ValueError("content_list_ambiguous")
    raw_bytes = candidates[0].read_bytes()
    try:
        content = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("content_list_invalid_json") from error
    return content, _digest(raw_bytes)


def _validate_component(value: str, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"invalid_{name}")
    return value


def _element_text(item: dict[str, Any]) -> str:
    """Create deterministic text for structured elements that have no text field."""
    text = str(item.get("text") or "").strip()
    if text or item.get("type") != "table":
        return text
    table = item.get("table_body")
    if isinstance(table, str):
        return table.strip()
    if isinstance(table, list):
        rows: list[str] = []
        for row in table:
            if isinstance(row, list):
                rows.append(" | ".join(str(cell or "").strip() for cell in row))
            elif isinstance(row, dict):
                rows.append(" | ".join(f"{key}: {value}" for key, value in sorted(row.items())))
            else:
                rows.append(str(row or "").strip())
        return "\n".join(row for row in rows if row)
    if isinstance(table, dict):
        return json.dumps(table, ensure_ascii=False, sort_keys=True)
    return ""


def _write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process(root: Path, *, document_id: str, version_id: str, output_root: Path,
            tenant_id: str, shop_id: str, max_chars: int = 700,
            task_id: str | None = None) -> dict[str, Any]:
    document_id = _validate_component(document_id, "document_id")
    version_id = _validate_component(version_id, "version_id")
    if task_id is not None:
        task_id = _validate_component(task_id, "task_id")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    raw, raw_sha256 = _load_content_list(root)
    normalized = normalize_content_list(raw, document_id=document_id, version_id=version_id)
    metadata = {
        "tenant_id": tenant_id,
        "shop_id": shop_id,
        "disclosure_class": "internal_only",
        "effective_from": None,
        "effective_to": None,
    }
    elements: list[ParsedElement] = []
    for item in normalized:
        text = _element_text(item)
        if not text and item["type"] != "image":
            continue
        position = dict(item["source_position"])
        if item.get("page_no") is not None:
            position["page_no"] = item["page_no"]
        elements.append(ParsedElement(
            document_id=document_id,
            document_version_id=version_id,
            title=document_id,
            heading=tuple(str(value) for value in item.get("heading_path", [])),
            content=text,
            source_position=position,
            metadata={**metadata, "mineru_type": item["type"], "warning": item["warning"],
                      "table_body": item.get("table_body") if item["type"] == "table" else None},
            disclosure_class=metadata["disclosure_class"],
            effective_from=None,
            effective_to=None,
            element_type=item["type"],
        ))
    chunks = chunk_elements_v2(elements, max_chars=max_chars)
    parsed_dir = output_root / "parsed" / document_id / version_id
    chunks_dir = output_root / "chunks"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    layout = {"document_id": document_id, "document_version_id": version_id,
              "elements": normalized}
    _write_atomic(parsed_dir / "layout.json",
                  json.dumps(layout, ensure_ascii=False, indent=2) + "\n")
    lines = []
    for item in normalized:
        text = _element_text(item)
        if text:
            if item.get("type") == "heading":
                level = int(item.get("text_level") or 1)
                lines.append("#" * max(1, min(level, 6)) + " " + text)
            else:
                lines.append(text)
    _write_atomic(parsed_dir / "full.md", "\n\n".join(lines) + "\n")
    chunk_path = chunks_dir / f"{document_id}-{version_id}.jsonl"
    chunk_lines = []
    for chunk in chunks:
        chunk_lines.append(json.dumps({
                "document_id": chunk.document_id,
                "document_version_id": chunk.version_id,
                "chunk_id": chunk.chunk_id,
                "content": chunk.content,
                "source_position": chunk.source_position,
                "split_reason": chunk.split_reason,
                "chunk_hash": chunk.chunk_hash,
                "chunk_rule_version": chunk.rule_version,
            }, ensure_ascii=False, sort_keys=True) + "\n")
    _write_atomic(chunk_path, "".join(chunk_lines))
    meta = {
        "report_version": "mineru-artifact-v1",
        "document_id": document_id,
        "document_version_id": version_id,
        "input_content_list_sha256": raw_sha256,
        "element_count": len(normalized),
        "chunk_count": len(chunks),
        "page_numbers": sorted({item["page_no"] for item in normalized if isinstance(item.get("page_no"), int)}),
        "table_count": sum(item["type"] == "table" for item in normalized),
        "image_count": sum(item["type"] == "image" for item in normalized),
        "warning_count": sum(bool(item.get("warning")) for item in normalized),
        "chunk_rule_version": chunks[0].rule_version if chunks else "structured-v2",
        "real_service_acceptance": False,
    }
    if task_id is not None:
        meta["task_id"] = task_id
    _write_atomic(parsed_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("/var/lib/ecommerce-rag/real-docs/real-docs-v1"))
    parser.add_argument("--tenant-id", default="mineru-lab")
    parser.add_argument("--shop-id", default="unassigned")
    parser.add_argument("--max-chars", type=int, default=700)
    parser.add_argument("--task-id")
    args = parser.parse_args()
    try:
        report = process(args.artifact_dir, document_id=args.document_id, version_id=args.version_id,
                         output_root=args.output_root, tenant_id=args.tenant_id,
                         shop_id=args.shop_id, max_chars=args.max_chars,
                         task_id=args.task_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "FAILED", "error_code": str(error), "real_service_acceptance": False}))
        return 1
    print(json.dumps({"status": "PASS", **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
