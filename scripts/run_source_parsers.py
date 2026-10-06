#!/usr/bin/env python3
"""Run deterministic local parsers over source fixtures without network access."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "python"))
from app.ingest.pipeline import chunk_elements_v2, parser_for
from scripts.validate_source_fixtures import safe_fixture_path

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(root: Path) -> dict:
    """Read only a well-formed fixture manifest before parsing any input."""
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest_invalid:{type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest_not_object")
    if not isinstance(manifest.get("fixtures"), list):
        raise ValueError("fixtures_must_be_array")
    return manifest


def run(root: Path) -> dict:
    manifest = load_manifest(root)
    reports = []
    for index, item in enumerate(manifest.get("fixtures", [])):
        if not isinstance(item, dict):
            raise ValueError(f"fixture_not_object:{index}")
        fixture_id = item.get("fixture_id", f"<fixture-{index}>")
        format_name = item.get("format")
        expected_status = item.get("expected_parse_status")
        path = safe_fixture_path(root, item.get("path"))
        result = {
            "fixture_id": fixture_id, "format": format_name,
            "expected_parse_status": expected_status,
            "parse_status": "FAILED", "element_count": 0, "chunk_count": 0,
            "warning_count": 0, "error_code": None, "source_position_coverage": 0,
            "source_path": None, "source_size_bytes": None, "source_sha256": None,
            "chunks": [],
        }
        try:
            if path is None:
                raise ValueError("unsafe_fixture_path")
            result["source_path"] = path.relative_to(root.resolve()).as_posix()
            result["source_size_bytes"] = path.stat().st_size
            result["source_sha256"] = file_sha256(path)
            if format_name not in {"markdown", "csv", "docx", "html"}:
                raise ValueError("unsupported_type")
            if format_name == "csv":
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.reader(handle))
                if rows:
                    width = len(rows[0])
                    if any(len(row) != width for row in rows[1:] if row):
                        raise ValueError("csv_column_mismatch")
            metadata = {
                "title": fixture_id, "tenant_id": "demo-tenant-a",
                "shop_id": "demo-shop-east", "disclosure_class": "internal_only",
                "effective_from": "2026-10-03", "effective_to": None,
            }
            elements = parser_for(format_name).parse(
                path, document_id=fixture_id, version_id="fixture-v1", metadata=metadata
            )
            if not elements:
                raise ValueError("empty_parse")
            chunks = chunk_elements_v2(elements, max_chars=700)
            result["parse_status"] = "PASS"
            result["element_count"] = len(elements)
            result["chunk_count"] = len(chunks)
            result["source_position_coverage"] = sum(bool(e.source_position) for e in elements)
            result["chunks"] = [{
                "document_version_id": c.version_id,
                "section_seq": c.section_seq,
                "section_chunk_index": c.chunk_index,
                "chunk_index": i,
                "content_type": c.content_type,
                "split_reason": c.split_reason,
                "chunk_hash": c.chunk_hash,
                "rule_version": c.rule_version,
                "source_position": c.source_position,
                "source_path": result["source_path"],
                "source_size_bytes": result["source_size_bytes"],
                "source_sha256": result["source_sha256"],
                "token_count": max(1, len(c.content.split())),
            } for i, c in enumerate(chunks)]
        except UnicodeDecodeError:
            result["error_code"] = "encoding_error"
        except (ValueError, KeyError, OSError) as exc:
            result["error_code"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - preserve parser failure code in report
            result["error_code"] = type(exc).__name__
        expected_ok = expected_status == "parseable"
        actual_ok = result["parse_status"] == "PASS"
        result["expectation_status"] = "PASS" if expected_ok == actual_ok else "MISMATCH"
        reports.append(result)
    return {
        "report_version": "source-fixtures-parse-v1",
        "root": str(root), "real_service_acceptance": False,
        "reports": reports,
        "summary": {
            "fixtures": len(reports),
            "passed": sum(r["parse_status"] == "PASS" for r in reports),
            "expected_failures": sum(r["expected_parse_status"] == "expected_failure" for r in reports),
            "mismatches": sum(r["expectation_status"] == "MISMATCH" for r in reports),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run(args.root)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        report = {
            "report_version": "source-fixtures-parse-v1",
            "root": str(args.root),
            "real_service_acceptance": False,
            "status": "FAIL",
            "error_code": str(exc),
            "reports": [],
            "summary": {"fixtures": 0, "passed": 0, "expected_failures": 0, "mismatches": 0},
        }
    output = args.output or args.root / "reports" / "parse-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report["summary"]}))
    return 0 if report.get("status") != "FAIL" and report["summary"]["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
