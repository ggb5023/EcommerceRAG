#!/usr/bin/env python3
"""Read-only validation and local parser/chunk report for source-fixtures-v1."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")
ALLOWED = {"parseable", "expected_failure"}
EXPECTED_SUFFIXES = {"markdown": {".md"}, "csv": {".csv"}, "docx": {".docx"}, "html": {".html"}}
FORMATS = set(EXPECTED_SUFFIXES) | {"invalid"}


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate manifest keys instead of silently keeping the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_fixture_path(root: Path, raw_path: object) -> Path | None:
    """Resolve a manifest path only when it stays inside the fixture root."""
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        return None
    relative = PurePosixPath(raw_path)
    if (relative.is_absolute() or relative.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in relative.parts)):
        return None
    root = root.resolve()
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None
    try:
        candidate.resolve().relative_to(root)
    except ValueError:
        return None
    return candidate


def validate(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    try:
        data = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"manifest_version": None, "fixture_count": 0,
                "errors": [f"manifest_invalid:{type(exc).__name__}"], "status": "FAIL"}
    if not isinstance(data, dict):
        return {"manifest_version": None, "fixture_count": 0,
                "errors": ["manifest_not_object"], "status": "FAIL"}
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list):
        return {"manifest_version": data.get("manifest_version"), "fixture_count": 0,
                "errors": ["fixtures_must_be_array"], "status": "FAIL"}
    errors: list[str] = []
    seen = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(fixtures):
        if not isinstance(item, dict):
            errors.append(f"fixture_not_object:{index}")
            continue
        fixture_id = item.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            errors.append(f"invalid_fixture_id:{index}")
            fixture_label = f"<fixture-{index}>"
        elif fixture_id in seen:
            errors.append(f"duplicate_fixture_id:{fixture_id}")
            fixture_label = fixture_id
        else:
            seen.add(fixture_id)
            fixture_label = fixture_id
        status = item.get("expected_parse_status")
        if status not in ALLOWED:
            errors.append(f"invalid_expected_parse_status:{fixture_label}")
        format_name = item.get("format")
        if format_name not in FORMATS:
            errors.append(f"invalid_format:{fixture_label}")
        raw_path = item.get("path")
        path = safe_fixture_path(root, raw_path)
        if path is None:
            errors.append(f"unsafe_path:{fixture_label}")
            continue
        normalized_path = path.relative_to(root.resolve()).as_posix()
        if normalized_path in seen_paths:
            errors.append(f"duplicate_path:{normalized_path}")
        seen_paths.add(normalized_path)
        if not path.is_file():
            errors.append(f"missing:{raw_path}")
            continue
        suffixes = EXPECTED_SUFFIXES.get(format_name)
        if suffixes is not None and path.suffix.lower() not in suffixes:
            errors.append(f"format_extension_mismatch:{fixture_label}")
        actual_hash = sha256(path)
        if actual_hash != item.get("sha256"):
            errors.append(f"sha256_mismatch:{fixture_label}")
        if path.stat().st_size != item.get("size_bytes"):
            errors.append(f"size_mismatch:{fixture_label}")
        tags = item.get("scenario_tags")
        if (not isinstance(tags, list)
                or not tags
                or not all(isinstance(tag, str) and tag.strip() for tag in tags)):
            errors.append(f"invalid_scenario_tags:{fixture_label}")
    return {"manifest_version": data.get("manifest_version"), "fixture_count": len(seen),
            "errors": errors, "status": "PASS" if not errors else "FAIL"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    report = validate(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
