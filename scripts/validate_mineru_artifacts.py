#!/usr/bin/env python3
"""Validate MinerU task, input, parsed and chunk artifact bindings.

The validator reads metadata and chunk records only.  It never sends requests,
rewrites artifacts, or treats a legacy artifact without the newer integrity
fields as a full pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _manifest_sources(root: Path) -> dict[str, dict[str, Any]]:
    manifest = _read_json(root / "manifest.json")
    sources = manifest.get("sources", [])
    if not isinstance(sources, list):
        return {}
    return {
        item["document_id"]: item
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("document_id"), str)
    }


def _chunk_path(root: Path, parsed_dir: Path, meta: dict[str, Any], document_id: str,
                version_id: str) -> tuple[Path, bool, str | None]:
    value = meta.get("chunk_path")
    if isinstance(value, str) and value:
        candidate = (root / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return candidate, True, "chunk_path_outside_root"
        return candidate, True, None
    return root / "chunks" / f"{document_id}-{version_id}.jsonl", False, None


def _check_chunks(path: Path, document_id: str, version_id: str) -> tuple[int, list[str]]:
    errors: list[str] = []
    count = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"chunk_invalid_json:{line_number}")
                    continue
                count += 1
                if not isinstance(item, dict):
                    errors.append(f"chunk_not_object:{line_number}")
                    continue
                if item.get("document_id") != document_id:
                    errors.append(f"chunk_document_id_mismatch:{line_number}")
                if item.get("document_version_id") != version_id:
                    errors.append(f"chunk_version_id_mismatch:{line_number}")
                if not _valid_hash(item.get("chunk_hash")):
                    errors.append(f"chunk_hash_missing:{line_number}")
    except OSError:
        errors.append("chunk_artifact_missing")
    return count, errors


def _check_image_assets(parsed_dir: Path, expected: Any) -> tuple[int, list[str]]:
    """Validate the optional image directory without following symlinks."""
    image_dir = parsed_dir / "images"
    if not image_dir.exists():
        return 0, ["image_assets_missing"]
    if image_dir.is_symlink() or not image_dir.is_dir():
        return 0, ["image_assets_invalid"]
    errors: list[str] = []
    count = 0
    for path in sorted(image_dir.rglob("*")):
        if path.is_symlink():
            errors.append("image_asset_symlink")
        elif path.is_file():
            count += 1
        elif not path.is_dir():
            errors.append("image_asset_not_regular")
    if expected is None:
        errors.append("image_asset_count_missing")
    elif isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        errors.append("image_asset_count_invalid")
    elif expected != count:
        errors.append("image_asset_count_mismatch")
    return count, errors


def _check_element_metadata(meta: dict[str, Any], elements: Any) -> tuple[list[str], list[str]]:
    """Cross-check summary counts against the normalized layout elements."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(elements, list):
        return errors, warnings

    element_rows = [item for item in elements if isinstance(item, dict)]
    if len(element_rows) != len(elements):
        errors.append("layout_element_not_object")

    actual_warning_count = sum(bool(item.get("warning")) for item in element_rows)
    actual_table_count = sum(item.get("type") == "table" for item in element_rows)
    actual_image_count = sum(item.get("type") == "image" for item in element_rows)
    actual_page_numbers = sorted({
        item["page_no"] for item in element_rows
        if isinstance(item.get("page_no"), int) and not isinstance(item.get("page_no"), bool)
    })

    for field, actual in (
        ("warning_count", actual_warning_count),
        ("table_count", actual_table_count),
        ("image_count", actual_image_count),
    ):
        declared = meta.get(field)
        if declared is None:
            warnings.append(f"{field}_missing")
        elif isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
            errors.append(f"{field}_invalid")
        elif declared != actual:
            errors.append(f"{field}_mismatch")

    declared_pages = meta.get("page_numbers")
    if declared_pages is None:
        warnings.append("page_numbers_missing")
    elif (
        not isinstance(declared_pages, list)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in declared_pages)
        or declared_pages != sorted(set(declared_pages))
    ):
        errors.append("page_numbers_invalid")
    elif declared_pages != actual_page_numbers:
        errors.append("page_numbers_mismatch")
    return errors, warnings


def validate(root: Path) -> dict[str, Any]:
    """Return a metadata-only consistency report for one artifact root."""
    root = root.resolve()
    sources = _manifest_sources(root)
    rows: list[dict[str, Any]] = []
    for meta_path in sorted((root / "parsed").glob("*/*/meta.json")):
        parsed_dir = meta_path.parent
        meta = _read_json(meta_path)
        document_id = meta.get("document_id")
        version_id = meta.get("document_version_id")
        row: dict[str, Any] = {
            "document_id": document_id,
            "document_version_id": version_id,
            "parsed_artifact_path": str(parsed_dir),
            "status": "FAIL",
            "errors": [],
            "warnings": [],
        }
        errors: list[str] = row["errors"]
        warnings: list[str] = row["warnings"]
        if not isinstance(document_id, str) or not isinstance(version_id, str):
            errors.append("document_identity_missing")
            rows.append(row)
            continue
        if parsed_dir.name != version_id or parsed_dir.parent.name != document_id:
            errors.append("parsed_path_identity_mismatch")

        source = sources.get(document_id)
        if not source:
            errors.append("source_manifest_entry_missing")
        else:
            raw_path = root / "raw" / str(source.get("file_name", ""))
            if not raw_path.is_file():
                errors.append("input_pdf_missing")
            else:
                actual_input_hash = _sha256(raw_path)
                row["input_pdf_sha256"] = actual_input_hash
                declared_input_hash = source.get("sha256")
                if not _valid_hash(declared_input_hash):
                    errors.append("source_manifest_sha256_invalid")
                elif declared_input_hash != actual_input_hash:
                    errors.append("source_manifest_sha256_mismatch")
                parsed_input_hash = meta.get("input_pdf_sha256")
                if parsed_input_hash is None:
                    warnings.append("input_pdf_sha256_missing")
                elif not _valid_hash(parsed_input_hash):
                    errors.append("input_pdf_sha256_invalid")
                elif parsed_input_hash != actual_input_hash:
                    errors.append("input_pdf_sha256_mismatch")

        task_id = meta.get("task_id")
        row["task_id"] = task_id
        if not isinstance(task_id, str) or not task_id:
            errors.append("task_id_missing")
            task = {}
        else:
            task = _read_json(root / "task-artifacts" / task_id / "meta.json")
            if not task:
                errors.append("task_artifact_missing")
            elif task.get("status") != "PASS":
                errors.append("task_artifact_not_pass")
        task_result_hash = task.get("result_sha256")
        if not _valid_hash(task_result_hash):
            errors.append("task_result_sha256_missing_or_invalid")
        parsed_result_hash = meta.get("result_sha256")
        if parsed_result_hash is None:
            warnings.append("result_sha256_missing")
        elif not _valid_hash(parsed_result_hash):
            errors.append("result_sha256_invalid")
        elif parsed_result_hash != task_result_hash:
            errors.append("result_sha256_mismatch")
        task_artifact_root = task.get("artifact_root")
        if task_artifact_root is not None and Path(str(task_artifact_root)).resolve() != (root / "task-artifacts" / str(task_id)).resolve():
            errors.append("task_artifact_root_mismatch")

        layout_path = parsed_dir / "layout.json"
        full_md_path = parsed_dir / "full.md"
        if not layout_path.is_file():
            errors.append("layout_missing")
        else:
            layout = _read_json(layout_path)
            elements = layout.get("elements")
            if not isinstance(elements, list):
                errors.append("layout_elements_missing")
            elif meta.get("element_count") != len(elements):
                errors.append("element_count_mismatch")
            element_errors, element_warnings = _check_element_metadata(meta, elements)
            errors.extend(element_errors)
            warnings.extend(element_warnings)
        if not full_md_path.is_file():
            errors.append("full_md_missing")

        content_list_path = parsed_dir / "content_list.json"
        if not content_list_path.is_file():
            warnings.append("content_list_missing")
        else:
            try:
                content_list_bytes = content_list_path.read_bytes()
                content_list = json.loads(content_list_bytes)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                errors.append("content_list_invalid")
            else:
                if not isinstance(content_list, list):
                    errors.append("content_list_not_array")
                declared_content_hash = meta.get("input_content_list_sha256")
                if not _valid_hash(declared_content_hash):
                    errors.append("input_content_list_sha256_missing_or_invalid")
                elif declared_content_hash != hashlib.sha256(content_list_bytes).hexdigest():
                    errors.append("input_content_list_sha256_mismatch")
        _, image_errors = _check_image_assets(parsed_dir, meta.get("image_asset_count"))
        if "image_assets_missing" in image_errors or "image_asset_count_missing" in image_errors:
            warnings.extend(image_errors)
            image_errors = [error for error in image_errors
                            if error not in {"image_assets_missing", "image_asset_count_missing"}]
        errors.extend(image_errors)

        chunk_path, explicit_chunk_path, chunk_path_error = _chunk_path(
            root, parsed_dir, meta, document_id, version_id
        )
        row["chunk_path"] = str(chunk_path)
        if chunk_path_error:
            errors.append(chunk_path_error)
        if not explicit_chunk_path:
            warnings.append("chunk_path_missing")
        chunk_count, chunk_errors = _check_chunks(chunk_path, document_id, version_id)
        errors.extend(chunk_errors)
        row["chunk_count"] = chunk_count
        if meta.get("chunk_count") != chunk_count:
            errors.append("chunk_count_mismatch")
        row["element_count"] = meta.get("element_count")
        row["result_sha256"] = task_result_hash
        if errors:
            row["status"] = "FAIL"
        elif warnings:
            row["status"] = "LEGACY_UNVERIFIED"
        else:
            row["status"] = "PASS"
        rows.append(row)

    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("PASS", "LEGACY_UNVERIFIED", "FAIL")}
    return {
        "report_version": "mineru-artifact-consistency-v1",
        "root": str(root),
        "real_service_acceptance": False,
        "documents": rows,
        "summary": {"documents": len(rows), **counts},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.root)
    output = args.output
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output) if output else None, **report["summary"]}, ensure_ascii=False))
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
