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
PLACEHOLDER = re.compile(r"合成案例\s*\d+|synthetic-point-\d+", re.IGNORECASE)


def validate(cases_path: Path, metadata_path: Path) -> tuple[list[dict], dict, list[str]]:
    errors: list[str] = []
    raw = cases_path.read_bytes()
    cases = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path("eval/synthetic_cases.jsonl"))
    parser.add_argument("--metadata", type=Path, default=Path("eval/synthetic_cases.metadata.json"))
    parser.add_argument("--review-output", type=Path)
    args = parser.parse_args()
    cases, metadata, errors = validate(args.cases, args.metadata)
    if errors:
        for error in errors: print(f"FAIL {error}")
        return 1
    counts = collections.Counter(tag for case in cases for tag in case["tags"])
    print(f"PASS synthetic eval: {len(cases)} cases; sha256={metadata['sha256']}")
    print("coverage=" + json.dumps(dict(sorted(counts.items())), ensure_ascii=False, sort_keys=True))
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
