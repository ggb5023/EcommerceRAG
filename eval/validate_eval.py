#!/usr/bin/env python3
"""Read-only validation for the synthetic evaluation set.

Use --review-output to explicitly generate a separate pending review checklist.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from datetime import date
from pathlib import Path

REQUIRED = {"case_id", "query", "expected_doc_ids", "expected_answer_points", "intent",
            "information_source", "tags", "authorization", "business_date", "source"}
REQUIRED_AUTH = {"tenant_id", "shop_id", "role"}
REQUIRED_SOURCE = {"type", "license", "source_version"}
REQUIRED_TAGS = {"product_knowledge", "policy", "negative", "spec_comparison", "multi_turn",
                 "factual", "freshness", "unanswerable", "unauthorized"}
REVIEW_STATUSES = {"pending", "approved", "needs_revision", "rejected"}
PLACEHOLDER = re.compile(r"合成案例\s*\d+|synthetic-point-\d+", re.IGNORECASE)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys instead of silently taking the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_review_json(raw: bytes) -> object:
    """Decode a review file without accepting duplicate keys or bad UTF-8."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"review file is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"review file invalid JSON: {exc}") from exc


def _load_case_jsonl(raw: bytes) -> tuple[list[dict], list[str]]:
    """Parse JSONL cases without allowing malformed rows to escape validation."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [], [f"cases are not valid UTF-8: {exc}"]

    cases: list[dict] = []
    errors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"case {line_number} invalid JSON: {exc}")
            continue
        if not isinstance(case, dict):
            errors.append(f"case {line_number} must be a JSON object")
            continue
        cases.append(case)
    return cases, errors


def validate(cases_path: Path, metadata_path: Path) -> tuple[list[dict], dict, list[str]]:
    errors: list[str] = []
    raw = cases_path.read_bytes()
    cases, parse_errors = _load_case_jsonl(raw)
    errors.extend(parse_errors)
    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return cases, {}, errors + [f"metadata invalid: {exc}"]
    if not isinstance(metadata, dict):
        return cases, {}, errors + ["metadata must be a JSON object"]
    if len(cases) != 60: errors.append(f"case_count={len(cases)}, expected 60")
    if metadata.get("case_count") != len(cases): errors.append("metadata case_count mismatch")
    if metadata.get("sha256") != hashlib.sha256(raw).hexdigest(): errors.append("metadata sha256 mismatch")
    ids = []
    docs = []
    seen_sources = set()
    for number, case in enumerate(cases, 1):
        missing = REQUIRED - case.keys()
        if missing: errors.append(f"case {number} missing {sorted(missing)}")
        ids.append(case.get("case_id")); docs.extend(case.get("expected_doc_ids", []))
        if PLACEHOLDER.search(str(case.get("query", ""))) or PLACEHOLDER.search(str(case.get("expected_answer_points", []))):
            errors.append(f"case {number} contains placeholder text")
        if not isinstance(case.get("expected_doc_ids"), list) or not case.get("expected_doc_ids"): errors.append(f"case {number} invalid expected_doc_ids")
        if not isinstance(case.get("expected_answer_points"), list) or not case.get("expected_answer_points"): errors.append(f"case {number} invalid answer points")
        if not REQUIRED_AUTH <= set(case.get("authorization", {})): errors.append(f"case {number} incomplete authorization")
        if not REQUIRED_SOURCE <= set(case.get("source", {})): errors.append(f"case {number} incomplete source")
        try: date.fromisoformat(case.get("business_date", ""))
        except ValueError: errors.append(f"case {number} invalid business_date")
        seen_sources.add(case.get("source", {}).get("source_version"))
    if len(ids) != len(set(ids)): errors.append("duplicate case_id")
    if not REQUIRED_TAGS <= {tag for case in cases for tag in case.get("tags", [])}: errors.append("required coverage tag missing")
    if len(seen_sources) != 1 or metadata.get("eval_set_version") not in seen_sources: errors.append("source version mismatch")
    return cases, metadata, errors


def validate_review(review_path: Path, cases: list[dict]) -> tuple[list[str], collections.Counter, collections.Counter]:
    errors: list[str] = []
    issue_counts = collections.Counter()
    try:
        review = _load_review_json(review_path.read_bytes())
    except (OSError, ValueError) as exc:
        return [f"review file unreadable: {exc}"], collections.Counter(), issue_counts
    if not isinstance(review, list):
        return ["review file must contain a JSON array"], collections.Counter(), issue_counts
    case_by_id = {
        case["case_id"]: case
        for case in cases
        if isinstance(case.get("case_id"), str)
    }
    review_ids: list[str] = []
    if len(review) != len(cases):
        errors.append(f"review_count={len(review)}, expected {len(cases)}")
    for row in review:
        if isinstance(row, dict) and isinstance(row.get("case_id"), str):
            review_ids.append(row["case_id"])
    if len(review_ids) != len(set(review_ids)):
        issue_counts["duplicate_case_id"] += 1
        errors.append("duplicate review case_id")
    if set(review_ids) != set(case_by_id):
        errors.append("review case_id set does not match cases")
    status_counts = collections.Counter()
    for number, row in enumerate(review, 1):
        if not isinstance(row, dict):
            errors.append(f"review row {number} is not an object")
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"review row {number} case_id must be a non-empty string")
        status = row.get("review_status")
        if isinstance(status, str):
            status_counts[status] += 1
        else:
            status_counts["<invalid>"] += 1
            errors.append(f"review row {number} review_status must be a string")
        if not isinstance(status, str) or status not in REVIEW_STATUSES:
            errors.append(f"review row {number} has invalid review_status")
        if not isinstance(row.get("review_notes"), str):
            errors.append(f"review row {number} review_notes must be a string")
        elif isinstance(status, str) and status in {"needs_revision", "rejected"} and not row["review_notes"].strip():
            issue_counts["missing_notes"] += 1
            errors.append(f"review row {number} missing notes for {status}")
        source = case_by_id.get(case_id)
        if source:
            for field in ("query", "expected_doc_ids", "expected_answer_points", "intent",
                          "information_source", "authorization"):
                if row.get(field) != source.get(field):
                    issue_counts["evidence_drift"] += 1
                    errors.append(f"review row {number} field {field} differs from source")
    return errors, status_counts, issue_counts


def main() -> int:
    parser = argparse.ArgumentParser()
    eval_dir = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=eval_dir / "synthetic_cases.jsonl")
    parser.add_argument("--metadata", type=Path, default=eval_dir / "synthetic_cases.metadata.json")
    parser.add_argument("--review", type=Path, default=eval_dir / "synthetic_cases.review.json",
                        help="read-only review checklist validation")
    parser.add_argument("--review-output", type=Path)
    args = parser.parse_args()
    cases, metadata, errors = validate(args.cases, args.metadata)
    if errors:
        for error in errors: print(f"FAIL {error}")
        return 1
    review_errors, review_counts, issue_counts = validate_review(args.review, cases)
    if review_errors:
        for error in review_errors: print(f"FAIL {error}")
        return 1
    counts = collections.Counter(tag for case in cases for tag in case["tags"])
    print(f"PASS synthetic eval: {len(cases)} cases; sha256={metadata['sha256']}")
    print("coverage=" + json.dumps(dict(sorted(counts.items())), ensure_ascii=False, sort_keys=True))
    print("review=" + json.dumps(dict(sorted(review_counts.items())), ensure_ascii=False, sort_keys=True))
    print("review_issues=" + json.dumps(dict(sorted(issue_counts.items())), ensure_ascii=False, sort_keys=True))
    unresolved = review_counts.get("needs_revision", 0) + review_counts.get("rejected", 0)
    state = "VERIFIED_BASELINE" if len(cases) == 60 and review_counts.get("approved", 0) == 60 else "PENDING_REVIEW"
    print(f"review_state={state}; unresolved={unresolved}")
    if args.review_output:
        review = [{"case_id": c["case_id"], "query": c["query"], "expected_doc_ids": c["expected_doc_ids"],
                   "expected_answer_points": c["expected_answer_points"], "intent": c["intent"],
                   "information_source": c["information_source"], "authorization": c["authorization"],
                   "review_status": "pending", "review_notes": ""} for c in cases]
        args.review_output.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"WROTE review checklist: {args.review_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
