#!/usr/bin/env python3
"""Create an immutable, explicitly edited revision of a JSONL eval set.

The source JSONL is never modified.  A revision map must name every changed
case and field, and the only permitted fields are the answer points and the
eval-set source version embedded in each case.  The output is written
atomically and carries bindings to the source bytes and metadata version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

ALLOWED_CASE_FIELDS = {"expected_answer_points"}
REQUIRED_CASE_COUNT = 60


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"case line {line_number} is not an object")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case line {line_number} has invalid case_id")
        if any(existing.get("case_id") == case_id for existing in rows):
            raise ValueError(f"duplicate case_id: {case_id}")
        rows.append(value)
    if len(rows) != REQUIRED_CASE_COUNT:
        raise ValueError(f"case count {len(rows)} != {REQUIRED_CASE_COUNT}")
    return rows


def _validate_metadata(metadata: dict[str, Any], cases_path: Path, cases: list[dict[str, Any]]) -> None:
    if metadata.get("case_count") != len(cases):
        raise ValueError("metadata case_count does not match source cases")
    if metadata.get("sha256") != sha256(cases_path):
        raise ValueError("metadata sha256 does not match source cases")
    version = metadata.get("eval_set_version")
    if not isinstance(version, str) or not version:
        raise ValueError("metadata eval_set_version is missing")


def _validate_revision_map(revision_map: dict[str, Any], source_ids: set[str]) -> dict[str, dict[str, Any]]:
    if revision_map.get("source_cases_sha256") is None:
        raise ValueError("revision map source_cases_sha256 is required")
    if not isinstance(revision_map.get("source_cases_sha256"), str) or len(revision_map["source_cases_sha256"]) != 64:
        raise ValueError("revision map source_cases_sha256 is malformed")
    target_version = revision_map.get("target_eval_set_version")
    if not isinstance(target_version, str) or not target_version or target_version == revision_map.get("source_eval_set_version"):
        raise ValueError("target_eval_set_version must be a new non-empty version")
    changes = revision_map.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("revision map changes must be a non-empty list")
    output: dict[str, dict[str, Any]] = {}
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise ValueError(f"changes[{index}] must be an object")
        case_id = change.get("case_id")
        if not isinstance(case_id, str) or case_id not in source_ids:
            raise ValueError(f"unknown case_id in revision map: {case_id!r}")
        if case_id in output:
            raise ValueError(f"duplicate revision case_id: {case_id}")
        fields = change.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError(f"revision fields missing for {case_id}")
        unknown = sorted(set(fields) - ALLOWED_CASE_FIELDS)
        if unknown:
            raise ValueError(f"unsupported revision fields for {case_id}: {','.join(unknown)}")
        points = fields.get("expected_answer_points")
        if not isinstance(points, list) or not points or not all(isinstance(point, str) and point.strip() for point in points):
            raise ValueError(f"expected_answer_points must be a non-empty string list: {case_id}")
        output[case_id] = {"expected_answer_points": points}
    return output


def build_revision(cases_path: Path, metadata_path: Path, revision_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    cases = load_cases(cases_path)
    metadata = load_json(metadata_path)
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    _validate_metadata(metadata, cases_path, cases)
    revision_map = load_json(revision_path)
    if not isinstance(revision_map, dict):
        raise ValueError("revision map must be an object")
    if revision_map.get("source_cases_sha256") != sha256(cases_path):
        raise ValueError("revision map source_cases_sha256 does not match source cases")
    if revision_map.get("source_eval_set_version") != metadata.get("eval_set_version"):
        raise ValueError("revision map source_eval_set_version does not match metadata")
    changes = _validate_revision_map(revision_map, {case["case_id"] for case in cases})
    updated: list[dict[str, Any]] = []
    for case in cases:
        value = dict(case)
        change = changes.get(case["case_id"])
        if change:
            value["expected_answer_points"] = list(change["expected_answer_points"])
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"case source is invalid: {case['case_id']}")
        value["source"] = dict(source, source_version=revision_map["target_eval_set_version"])
        updated.append(value)
    target_metadata = dict(metadata)
    target_metadata["eval_set_version"] = revision_map["target_eval_set_version"]
    target_metadata["source_eval_set_version"] = metadata["eval_set_version"]
    target_metadata["source_cases_sha256"] = sha256(cases_path)
    target_metadata["case_count"] = len(updated)
    target_metadata["sha256"] = None
    return revision_map, updated, target_metadata


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_revision(cases_path: Path, metadata_path: Path, revision_path: Path, output_cases: Path, output_metadata: Path) -> dict[str, Any]:
    revision_map, updated, metadata = build_revision(cases_path, metadata_path, revision_path)
    if output_cases.resolve() == cases_path.resolve() or output_metadata.resolve() == metadata_path.resolve():
        raise ValueError("refusing to overwrite source cases or metadata")
    payload = "".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in updated).encode("utf-8")
    metadata["sha256"] = hashlib.sha256(payload).hexdigest()
    _atomic_write(output_cases, payload)
    _atomic_write(output_metadata, (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {
        "eval_set_version": metadata["eval_set_version"],
        "case_count": len(updated),
        "source_cases_sha256": revision_map["source_cases_sha256"],
        "cases_sha256": metadata["sha256"],
        "real_service_acceptance": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--revision-map", type=Path, required=True)
    parser.add_argument("--output-cases", type=Path, required=True)
    parser.add_argument("--output-metadata", type=Path, required=True)
    args = parser.parse_args()
    if args.output_cases.exists() or args.output_metadata.exists():
        raise SystemExit("refusing to overwrite existing revision outputs")
    report = write_revision(args.cases, args.metadata, args.revision_map, args.output_cases, args.output_metadata)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
