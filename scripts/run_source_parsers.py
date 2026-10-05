#!/usr/bin/env python3
"""Run deterministic local parsers over source fixtures without network access."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))
from app.ingest.pipeline import chunk_elements_v2, parser_for

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")


def run(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    reports = []
    for item in manifest["fixtures"]:
        fixture_id = item["fixture_id"]
        path = root / item["path"]
        result = {
            "fixture_id": fixture_id, "format": item["format"],
            "expected_parse_status": item["expected_parse_status"],
            "parse_status": "FAILED", "element_count": 0, "chunk_count": 0,
            "warning_count": 0, "error_code": None, "source_position_coverage": 0,
            "chunks": [],
        }
        try:
            if item["format"] not in {"markdown", "csv", "docx", "html"}:
                raise ValueError("unsupported_type")
            if item["format"] == "csv":
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
            elements = parser_for(item["format"]).parse(
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
                "token_count": max(1, len(c.content.split())),
            } for i, c in enumerate(chunks)]
        except UnicodeDecodeError:
            result["error_code"] = "encoding_error"
        except (ValueError, KeyError, OSError) as exc:
            result["error_code"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - preserve parser failure code in report
            result["error_code"] = type(exc).__name__
        expected_ok = item["expected_parse_status"] == "parseable"
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
    report = run(args.root)
    output = args.output or args.root / "reports" / "parse-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report["summary"]}))
    return 0 if report["summary"]["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
