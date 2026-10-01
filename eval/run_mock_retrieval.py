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
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    cases = load_cases(args.cases, metadata.get("sha256"))
    counts = collections.Counter(classify(case) for case in cases)
    coverage = collections.Counter()
    for case in cases:
        coverage.update(case.get("tags", []))
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
            "evidence_coverage_cases": sum(bool(case.get("expected_doc_ids")) for case in cases),
        },
        "tag_coverage": dict(sorted(coverage.items())),
        "notes": ["Local deterministic fixture classification only; no model, network, or customer data."],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
