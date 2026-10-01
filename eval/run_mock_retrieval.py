#!/usr/bin/env python3
"""Run a deterministic, local-only retrieval baseline over the synthetic set."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from datetime import datetime, timezone
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


def input_drift_issues(cases: list[dict], metadata: dict) -> list[str]:
    issues: list[str] = []
    expected_version = metadata.get("eval_set_version")
    expected_source = metadata.get("source_type")
    for case in cases:
        source = case.get("source", {})
        case_id = case.get("case_id", "<missing>")
        if source.get("source_version") != expected_version:
            issues.append(f"source_version_drift:{case_id}")
        if source.get("type") != expected_source:
            issues.append(f"source_type_drift:{case_id}")
    return issues


def authorization_issues(cases: list[dict]) -> list[str]:
    issues: list[str] = []
    for case in cases:
        tags = set(case.get("tags", []))
        auth = case.get("authorization", {})
        case_id = case.get("case_id", "<missing>")
        if not all(isinstance(auth.get(field), str) and auth.get(field) for field in ("tenant_id", "shop_id", "role")):
            issues.append(f"incomplete_authorization:{case_id}")
        if "unauthorized" in tags and not case.get("expected_doc_ids"):
            issues.append(f"unauthorized_missing_target_evidence:{case_id}")
    return issues


def semantic_issues(cases: list[dict]) -> list[str]:
    issues: list[str] = []
    for case in cases:
        case_id = case.get("case_id", "<missing>")
        kind = classify(case)
        points = case.get("expected_answer_points")
        if not isinstance(points, list) or not all(isinstance(point, str) and point.strip() for point in points):
            issues.append(f"invalid_answer_points:{case_id}")
        if kind == "clarification":
            tags = set(case.get("tags", []))
            if "multi_turn" not in tags or not case.get("expected_doc_ids"):
                issues.append(f"incomplete_clarification_evidence:{case_id}")
        if kind in {"refusal", "unauthorized"} and not points:
            issues.append(f"missing_refusal_guidance:{case_id}")
    return issues


def m2_gate_status() -> dict[str, object]:
    """Declare external-input readiness without reading secrets or network state."""
    requirements = {
        "identity_roles_revocation": {"ready": False, "owner": "identity_owner", "evidence": "identity source, role catalog, revocation SLA"},
        "tenant_shop_mapping": {"ready": False, "owner": "business_owner", "evidence": "approved tenant/shop mapping"},
        "material_authorization_external_allowed": {"ready": False, "owner": "business_owner", "evidence": "approved material authorization and external_allowed list"},
        "business_date_rules": {"ready": False, "owner": "business_owner", "evidence": "effective-date and freshness rules"},
        "provider_endpoint_region_models": {"ready": False, "owner": "ai_cloud_owner", "evidence": "endpoint, region and four model IDs"},
        "provider_embedding_quota_usage_request_id": {"ready": False, "owner": "ai_cloud_owner", "evidence": "1024 dimension, quota, usage and request ID contract"},
        "material_versions_license_redaction": {"ready": False, "owner": "data_owner", "evidence": "material versions, licenses and redaction rules"},
    }
    return {
        "status": "BLOCKED",
        "real_service_acceptance": False,
        "requirements": requirements,
        "missing": sorted(key for key, value in requirements.items() if not value["ready"]),
    }


def load_fixture_doc_ids(path: Path | None) -> set[str] | None:
    """Load an optional, metadata-only document ID fixture.

    The synthetic case file intentionally contains no document bodies. A
    missing fixture therefore means retrieval metrics are not runnable rather
    than an implicit all-documents hit.
    """
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        ids = payload.get("document_ids")
    else:
        ids = payload
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        raise ValueError("fixture document_ids must be a list of non-empty strings")
    return set(ids)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_results(cases: list[dict], fixture_doc_ids: set[str] | None = None) -> list[dict]:
    """Return stable, metadata-only per-case results for audit and diffing."""
    results = []
    for case in cases:
        expected_docs = case.get("expected_doc_ids", [])
        kind = classify(case)
        hit_docs = sorted(set(expected_docs) & fixture_doc_ids) if fixture_doc_ids is not None else []
        results.append({
            "case_id": case.get("case_id"),
            "classification": kind,
            "expected_doc_count": len(expected_docs),
            "hit_doc_count": len(hit_docs),
            "evidence_expected": bool(expected_docs),
            "tags": sorted(case.get("tags", [])),
            "authorization_scope": case.get("authorization", {}).get("scope"),
            "status": (
                "PASS" if kind in {"refusal", "unauthorized", "clarification"}
                else "PASS" if fixture_doc_ids is not None and hit_docs
                else "NOT_RUN" if fixture_doc_ids is None
                else "FAIL"
            ),
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
    parser.add_argument("--fixture-doc-ids", type=Path, help="Optional JSON document ID fixture; no document bodies")
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    cases = load_cases(args.cases, metadata.get("sha256"))
    fixture_doc_ids = load_fixture_doc_ids(args.fixture_doc_ids)
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
    issues.extend(input_drift_issues(cases, metadata))
    issues.extend(authorization_issues(cases))
    issues.extend(semantic_issues(cases))
    retrieval_total = counts["retrieval"]
    evidence_total = sum(bool(case.get("expected_doc_ids")) for case in cases)
    retrieval_cases = [case for case in cases if classify(case) == "retrieval"]
    retrieval_with_hits = (
        sum(bool(set(case.get("expected_doc_ids", [])) & fixture_doc_ids) for case in retrieval_cases)
        if fixture_doc_ids is not None
        else None
    )
    expected_doc_total = sum(len(set(case.get("expected_doc_ids", []))) for case in retrieval_cases)
    hit_doc_total = (
        sum(len(set(case.get("expected_doc_ids", [])) & fixture_doc_ids) for case in retrieval_cases)
        if fixture_doc_ids is not None
        else None
    )
    missing_fixture_docs = (
        sorted({
            doc_id
            for case in retrieval_cases
            for doc_id in case.get("expected_doc_ids", [])
            if doc_id not in fixture_doc_ids
        })
        if fixture_doc_ids is not None
        else None
    )
    result = {
        "evaluation": "deterministic_mock_retrieval_baseline",
        "eval_set_version": metadata.get("eval_set_version"),
        "case_count": len(cases),
        "input_sha256": metadata.get("sha256"),
        "pipeline_version": metadata.get("pipeline_version"),
        "run": {
            "mode": "deterministic_mock",
            "run_id": "local-deterministic-" + metadata.get("sha256", "unknown")[:12],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "fixture_sha256": file_sha256(args.fixture_doc_ids) if args.fixture_doc_ids else None,
        },
        "real_service_acceptance": False,
        "m2_gate": m2_gate_status(),
        "results": {
            "retrieval_cases": counts["retrieval"],
            "refusal_cases": counts["refusal"],
            "unauthorized_cases": counts["unauthorized"],
            "clarification_cases": counts["clarification"],
            "expected_evidence_cases": evidence_total,
            "expected_evidence_rate": round(evidence_total / len(cases), 4) if cases else 0,
            "retrieval_cases_with_expected_docs": sum(
                bool(case.get("expected_doc_ids"))
                for case in cases
                if classify(case) == "retrieval"
            ),
            "retrieval_case_rate": round(retrieval_total / len(cases), 4) if cases else 0,
            "retrieval_hit_cases": retrieval_with_hits,
            "retrieval_hit_rate": (
                round(retrieval_with_hits / len(retrieval_cases), 4)
                if retrieval_with_hits is not None and retrieval_cases else None
            ),
            "expected_doc_coverage_rate": (
                round(hit_doc_total / expected_doc_total, 4)
                if hit_doc_total is not None and expected_doc_total else None
            ),
        },
        "retrieval_fixture": {
            "path": str(args.fixture_doc_ids) if args.fixture_doc_ids else None,
            "status": "READY" if fixture_doc_ids is not None else "NOT_RUN",
            "document_id_count": len(fixture_doc_ids) if fixture_doc_ids is not None else None,
            "missing_expected_document_ids": missing_fixture_docs,
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
        per_case = case_results(cases, fixture_doc_ids)
        result["case_results"] = per_case
        result["case_status_counts"] = dict(sorted(collections.Counter(row["status"] for row in per_case).items()))
        result["failed_case_ids"] = [row["case_id"] for row in per_case if row["status"] == "FAIL"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
