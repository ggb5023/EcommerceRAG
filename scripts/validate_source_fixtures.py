#!/usr/bin/env python3
"""Read-only validation and local parser/chunk report for source-fixtures-v1."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")
ALLOWED = {"parseable", "expected_failure"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    seen = set()
    for item in data.get("fixtures", []):
        fixture_id = item.get("fixture_id")
        if not fixture_id or fixture_id in seen:
            errors.append(f"duplicate_or_missing_fixture_id:{fixture_id}")
        seen.add(fixture_id)
        status = item.get("expected_parse_status")
        if status not in ALLOWED:
            errors.append(f"invalid_expected_parse_status:{fixture_id}")
        path = root / item["path"]
        if not path.is_file():
            errors.append(f"missing:{item['path']}")
            continue
        actual_hash = sha256(path)
        if actual_hash != item.get("sha256"):
            errors.append(f"sha256_mismatch:{fixture_id}")
        if path.stat().st_size != item.get("size_bytes"):
            errors.append(f"size_mismatch:{fixture_id}")
        if not item.get("scenario_tags"):
            errors.append(f"missing_scenario_tags:{fixture_id}")
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
