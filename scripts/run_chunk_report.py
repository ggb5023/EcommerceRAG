#!/usr/bin/env python3
"""Write a metadata-only chunk report from the local parser report.

The parser report is an input artifact, so successful aggregation first verifies
that its source traceability still points at the immutable fixture bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_path(root: Path, raw_path: object) -> Path | None:
    """Resolve a report path while rejecting traversal and symlink aliases."""
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        return None
    candidate = root.resolve() / Path(raw_path)
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if relative.as_posix() != raw_path or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return None
    current = root.resolve()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None
    return candidate


def validate_source_traceability(data: object, root: Path) -> list[str]:
    """Return explicit errors for malformed or tampered successful reports."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["parse_report_not_object"]
    if data.get("real_service_acceptance") is not False:
        errors.append("real_service_acceptance_must_be_false")
    reports = data.get("reports")
    if not isinstance(reports, list):
        return ["parse_report_reports_not_array"]
    summary = data.get("summary")
    if not isinstance(summary, dict):
        errors.append("parse_report_summary_not_object")
    else:
        for field in ("fixtures", "passed", "expected_failures", "mismatches"):
            if isinstance(summary.get(field), bool) or not isinstance(summary.get(field), int):
                errors.append(f"parse_report_summary_invalid:{field}")
    seen_fixture_ids: set[str] = set()
    for report_index, report in enumerate(reports):
        if not isinstance(report, dict):
            errors.append(f"report_not_object:{report_index}")
            continue
        fixture_id = report.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            errors.append(f"report_fixture_id_invalid:{report_index}")
            fixture_id = f"<report-{report_index}>"
        elif fixture_id in seen_fixture_ids:
            errors.append(f"report_fixture_id_duplicate:{fixture_id}")
        else:
            seen_fixture_ids.add(fixture_id)
        counts: dict[str, object] = {
            "element_count": report.get("element_count"),
            "chunk_count": report.get("chunk_count"),
            "warning_count": report.get("warning_count"),
            "source_position_coverage": report.get("source_position_coverage"),
        }
        for field, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"parse_report_count_invalid:{field}:{fixture_id}")
        if report.get("parse_status") not in {"PASS", "FAILED"}:
            errors.append(f"report_parse_status_invalid:{fixture_id}")
            continue
        if report.get("parse_status") != "PASS":
            continue
        label = fixture_id
        element_count = counts["element_count"]
        chunk_count = counts["chunk_count"]
        warning_count = counts["warning_count"]
        source_position_coverage = counts["source_position_coverage"]
        if all(isinstance(value, int) and not isinstance(value, bool)
               for value in (element_count, chunk_count, warning_count, source_position_coverage)):
            if chunk_count < element_count:
                errors.append(f"parse_report_count_order_invalid:chunk_count:{label}")
            if warning_count > element_count:
                errors.append(f"parse_report_count_order_invalid:warning_count:{label}")
            if source_position_coverage > element_count:
                errors.append(f"parse_report_count_order_invalid:source_position_coverage:{label}")
        source_path = report.get("source_path")
        source = _source_path(root, source_path)
        if source is None:
            errors.append(f"source_path_unsafe:{label}")
            continue
        if not source.exists() or not source.is_file():
            errors.append(f"source_missing:{label}")
            continue
        actual_size = source.stat().st_size
        actual_sha = file_sha256(source)
        reported_size = report.get("source_size_bytes")
        if isinstance(reported_size, bool) or not isinstance(reported_size, int):
            errors.append(f"source_size_invalid:{label}")
        elif reported_size != actual_size:
            errors.append(f"source_size_mismatch:{label}")
        reported_sha = report.get("source_sha256")
        if not isinstance(reported_sha, str) or reported_sha != actual_sha:
            errors.append(f"source_sha256_mismatch:{label}")
        chunks = report.get("chunks")
        if not isinstance(chunks, list):
            errors.append(f"chunks_not_array:{label}")
            continue
        if isinstance(chunk_count, int) and not isinstance(chunk_count, bool) and chunk_count != len(chunks):
            errors.append(f"parse_report_count_mismatch:chunk_count:{label}")
        for chunk_index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                errors.append(f"chunk_not_object:{label}:{chunk_index}")
                continue
            if (
                chunk.get("source_path") != source_path
                or chunk.get("source_size_bytes") != reported_size
                or chunk.get("source_sha256") != reported_sha
            ):
                errors.append(f"chunk_source_mismatch:{label}:{chunk_index}")
    if isinstance(summary, dict) and not errors:
        actual = {
            "fixtures": len(reports),
            "passed": sum(item.get("parse_status") == "PASS" for item in reports if isinstance(item, dict)),
            "expected_failures": sum(
                item.get("expected_parse_status") == "expected_failure"
                for item in reports if isinstance(item, dict)
            ),
            "mismatches": sum(
                item.get("expectation_status") == "MISMATCH"
                for item in reports if isinstance(item, dict)
            ),
        }
        for field, value in actual.items():
            if summary.get(field) != value:
                errors.append(f"parse_report_summary_mismatch:{field}")
    return errors


def _failure_report(root: Path, error: str, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "report_version": "source-fixtures-chunk-v2",
        "root": str(root),
        "status": "FAIL",
        "error_code": error,
        "errors": errors or [error],
        "real_service_acceptance": False,
        "chunks": [],
    }


def build_report(root: Path) -> dict[str, Any]:
    source = root / "reports" / "parse-report.json"
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"parse_report_invalid:{type(exc).__name__}") from exc
    errors = validate_source_traceability(data, root)
    if errors:
        raise ValueError("source_traceability_invalid")
    chunks = []
    for report in data["reports"]:
        if report["parse_status"] != "PASS":
            continue
        for item in report["chunks"]:
            chunks.append({"fixture_id": report["fixture_id"], **item})
    rule_versions = sorted({item.get("rule_version") for item in chunks if item.get("rule_version")})
    mixed_versions = len(rule_versions) > 1
    canonical = json.dumps(chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "report_version": "source-fixtures-chunk-v2",
        "root": str(root),
        "status": "PASS" if not mixed_versions else "FAIL",
        "error_code": "mixed_rule_versions" if mixed_versions else None,
        "errors": ["mixed_rule_versions"] if mixed_versions else [],
        "chunk_rule_version": rule_versions[0] if len(rule_versions) == 1 else None,
        "chunk_rule_versions": rule_versions,
        "mixed_rule_versions": mixed_versions,
        "real_service_acceptance": False,
        "fixture_count": data["summary"]["fixtures"],
        "successful_fixture_count": data["summary"]["passed"],
        "chunk_count": len(chunks),
        "chunk_report_sha256": hashlib.sha256(canonical).hexdigest(),
        "chunks": chunks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors: list[str] = []
    try:
        output = build_report(args.root)
    except ValueError as exc:
        error = str(exc)
        errors = [error]
        try:
            data = json.loads((args.root / "reports" / "parse-report.json").read_text(encoding="utf-8"))
            errors = validate_source_traceability(data, args.root) or errors
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        output = _failure_report(args.root, error, errors)
    path = args.root / "reports" / "chunk-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(path),
        "status": output["status"],
        "errors": output.get("errors", []),
        "chunks": output.get("chunk_count", 0),
        "sha256": output.get("chunk_report_sha256"),
    }))
    return 0 if output["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
