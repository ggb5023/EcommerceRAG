#!/usr/bin/env python3
"""Run a deterministic, local-only retrieval baseline over the synthetic set."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


def load_cases(path: Path, expected_sha: str | None) -> list[dict]:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if expected_sha and actual != expected_sha:
        raise ValueError(f"cases sha256 mismatch: {actual}")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def evaluate_cases(cases: list[dict]) -> tuple[collections.Counter, collections.Counter, list[str]]:
    counts = collections.Counter(classify(case) for case in cases)
    coverage = collections.Counter()
    issues: list[str] = []
    seen_ids: set[str] = set()
    for case in cases:
        case_id = case.get("case_id", "")
        if not case_id or case_id in seen_ids:
            issues.append(f"duplicate_or_missing_case_id:{case_id or '<missing>'}")
        seen_ids.add(case_id)
        expected_docs = case.get("expected_doc_ids", [])
        if not expected_docs:
            tags = set(case.get("tags", []))
            if not ({"unanswerable", "negative", "unauthorized"} & tags):
                issues.append(f"missing_expected_docs:{case_id}")
        coverage.update(case.get("tags", []))
    return counts, coverage, issues


def case_results(cases: list[dict]) -> list[dict]:
    """Return stable, metadata-only per-case results for audit and diffing."""
    results = []
    for case in cases:
        expected_docs = case.get("expected_doc_ids", [])
        kind = classify(case)
        results.append({
            "case_id": case.get("case_id"),
            "classification": kind,
            "expected_doc_count": len(expected_docs),
            "evidence_expected": bool(expected_docs),
            "tags": sorted(case.get("tags", [])),
            "authorization_scope": case.get("authorization", {}).get("scope"),
            "status": "PASS" if expected_docs or kind in {"refusal", "unauthorized", "clarification"} else "FAIL",
        })
    return results


def classify(case: dict) -> str:
    tags = set(case.get("tags", []))
    if "unauthorized" in tags:
        return "unauthorized"
    if "unanswerable" in tags or "negative" in tags:
        return "refusal"
    if "multi_turn" in tags:
        return "clarification"
    return "retrieval"


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=root / "synthetic_cases.jsonl")
    parser.add_argument("--metadata", type=Path, default=root / "synthetic_cases.metadata.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-cases", action="store_true", help="Include metadata-only per-case results")
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    cases = load_cases(args.cases, metadata.get("sha256"))
    counts, coverage, issues = evaluate_cases(cases)
    expected_case_count = metadata.get("case_count")
    if expected_case_count is not None and len(cases) != expected_case_count:
        issues.append(f"case_count_mismatch:{len(cases)}!={expected_case_count}")
    tag_conflicts = []
    for case in cases:
        tags = set(case.get("tags", []))
        if "authorized" in tags and "unauthorized" in tags:
            tag_conflicts.append(case.get("case_id", "<missing>"))
    issues.extend(f"conflicting_tags:{case_id}" for case_id in tag_conflicts)
    retrieval_total = counts["retrieval"]
    evidence_total = sum(bool(case.get("expected_doc_ids")) for case in cases)
    result = {
        "evaluation": "deterministic_mock_retrieval_baseline",
        "eval_set_version": metadata.get("eval_set_version"),
        "case_count": len(cases),
        "input_sha256": metadata.get("sha256"),
        "real_service_acceptance": False,
        "results": {
            "retrieval_cases": counts["retrieval"],
            "refusal_cases": counts["refusal"],
            "unauthorized_cases": counts["unauthorized"],
            "clarification_cases": counts["clarification"],
            "evidence_coverage_cases": evidence_total,
            "evidence_coverage_rate": round(evidence_total / len(cases), 4) if cases else 0,
            "retrieval_cases_with_expected_docs": sum(
                bool(case.get("expected_doc_ids"))
                for case in cases
                if classify(case) == "retrieval"
            ),
            "retrieval_case_rate": round(retrieval_total / len(cases), 4) if cases else 0,
        },
        "tag_coverage": dict(sorted(coverage.items())),
        "input_integrity": {
            "expected_case_count": expected_case_count,
            "actual_case_count": len(cases),
            "issues": issues,
            "status": "PASS" if not issues else "FAIL",
        },
        "notes": ["Local deterministic fixture classification only; no model, network, or customer data."],
    }
    if args.include_cases:
        result["case_results"] = case_results(cases)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
