#!/usr/bin/env python3
"""Build a review-only mapping between evaluation labels and source documents.

The proposal is derived from the independent source manifest's explicit
``expected_doc_id`` field.  It never changes the immutable evaluation JSONL,
copies query or answer text, or marks the mapping approved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ALLOWED_STATUS = "PENDING_REVIEW"
ALLOWED_DISCLOSURE_CLASSES = {"external_allowed", "internal_only", "unclassified"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"case line {line_number} is not an object")
        rows.append(value)
    return rows


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise TypeError("source manifest must contain a documents list")
    documents = value["documents"]
    if not all(isinstance(document, dict) for document in documents):
        raise ValueError("source manifest documents must be objects")
    return documents


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"invalid date value: {value!r}")
    return date.fromisoformat(value)


def _date_state(document: dict[str, Any], business_date: Any) -> str:
    current = _parse_date(business_date)
    if current is None:
        return "invalid_business_date"
    start = _parse_date(document.get("effective_from"))
    end = _parse_date(document.get("effective_to"))
    if start and current < start:
        return "not_yet_effective"
    if end and current >= end:
        return "expired_or_revoked"
    return "active"


def _risk_reasons(document: dict[str, Any], authorization: dict[str, Any], date_state: str) -> list[str]:
    reasons: list[str] = []
    if (
        document.get("tenant_id") != authorization.get("tenant_id")
        or document.get("shop_id") != authorization.get("shop_id")
    ):
        reasons.append("scope_mismatch")
    disclosure = document.get("disclosure_class")
    if disclosure not in ALLOWED_DISCLOSURE_CLASSES:
        reasons.append("unclassified")
    elif disclosure == "internal_only" and authorization.get("role") not in {"admin", "owner"}:
        reasons.append("internal_only")
    if date_state != "active":
        reasons.append(date_state)
    return reasons


def _load_corpus(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    corpus: dict[str, dict[str, Any]] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("document_id"), str):
            raise TypeError(f"corpus line {line_number} is not a document object")
        if value["document_id"] in corpus:
            raise ValueError(f"duplicate corpus document_id: {value['document_id']}")
        corpus[value["document_id"]] = value
    return corpus


def build_proposal(cases_path: Path, manifest_path: Path, corpus_path: Path | None = None) -> dict[str, Any]:
    cases = _load_cases(cases_path)
    documents = _load_manifest(manifest_path)
    corpus = _load_corpus(corpus_path)
    by_expected: dict[str, dict[str, Any]] = {}
    duplicate_expected: set[str] = set()
    for document in documents:
        expected_id = document.get("expected_doc_id")
        source_id = document.get("document_id")
        if not isinstance(expected_id, str) or not expected_id:
            raise ValueError("every source document needs a non-empty expected_doc_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("every source document needs a non-empty document_id")
        if expected_id in by_expected:
            duplicate_expected.add(expected_id)
        by_expected[expected_id] = document
    if duplicate_expected:
        raise ValueError("duplicate expected_doc_id: " + ",".join(sorted(duplicate_expected)))

    mapping: dict[str, dict[str, str]] = {}
    case_checks: list[dict[str, Any]] = []
    issues: list[str] = []
    case_ids: list[str] = []
    for case in cases:
        case_id = case.get("case_id")
        expected_ids = case.get("expected_doc_ids")
        authorization = case.get("authorization")
        if not isinstance(case_id, str) or not case_id:
            issues.append("case_id_invalid")
            continue
        case_ids.append(case_id)
        if not isinstance(expected_ids, list) or not all(isinstance(value, str) and value for value in expected_ids):
            issues.append(f"{case_id}:expected_doc_ids_invalid")
            continue
        if not isinstance(authorization, dict):
            issues.append(f"{case_id}:authorization_invalid")
            continue
        case_mapping: dict[str, str] = {}
        document_checks: list[dict[str, Any]] = []
        for expected_id in expected_ids:
            document = by_expected.get(expected_id)
            if document is None:
                issues.append(f"{case_id}:missing_expected_doc:{expected_id}")
                continue
            source_id = document["document_id"]
            case_mapping[expected_id] = source_id
            corpus_document = corpus.get(source_id, {})
            corpus_chunks = corpus_document.get("chunks", []) if isinstance(corpus_document, dict) else []
            scope_match = (
                document.get("tenant_id") == authorization.get("tenant_id")
                and document.get("shop_id") == authorization.get("shop_id")
            )
            date_state = _date_state(document, case.get("business_date"))
            risk_reasons = _risk_reasons(document, authorization, date_state)
            document_checks.append(
                {
                    "expected_doc_id": expected_id,
                    "source_document_id": source_id,
                    "document_version_id": corpus_document.get("document_version_id") or document.get("document_version_id"),
                    "source_version": corpus_document.get("source_version") or "ecommerce-demo-v1",
                    "source_sha256": corpus_document.get("source_sha256") or document.get("sha256"),
                    "evidence_chunks": [
                        {
                            "chunk_id": chunk.get("chunk_id"),
                            "source_position": chunk.get("source_position"),
                        }
                        for chunk in corpus_chunks if isinstance(chunk, dict)
                    ],
                    "scope_match": scope_match,
                    "date_state": date_state,
                    "disclosure_class": document.get("disclosure_class")
                    if document.get("disclosure_class") in ALLOWED_DISCLOSURE_CLASSES
                    else "unclassified",
                    "review_required": bool(risk_reasons),
                    "risk_reasons": risk_reasons,
                }
            )
            if not scope_match:
                issues.append(f"{case_id}:scope_mismatch:{expected_id}")
        if case_mapping:
            mapping[case_id] = case_mapping
        case_checks.append(
            {
                "case_id": case_id,
                "expected_doc_ids": expected_ids,
                "expected_doc_count": len(expected_ids),
                "mapped_doc_count": len(case_mapping),
                "document_checks": document_checks,
            }
        )

    duplicate_cases = sorted(case_id for case_id, count in Counter(case_ids).items() if count > 1)
    issues.extend(f"duplicate_case_id:{case_id}" for case_id in duplicate_cases)
    if len(cases) != 60:
        issues.append(f"case_count:{len(cases)}!=60")

    risk_counts: Counter[str] = Counter()
    risk_case_count = 0
    for check in case_checks:
        reasons = {reason for document in check["document_checks"] for reason in document["risk_reasons"]}
        if reasons:
            risk_case_count += 1
            risk_counts.update(reasons)

    return {
        "alignment_version": "synthetic-m2-v1-to-ecommerce-m2-aligned-v1-v1",
        "status": ALLOWED_STATUS,
        "eval_set_version": "synthetic-m2-v1",
        "cases_sha256": _sha256(cases_path),
        "source_manifest_sha256": _sha256(manifest_path),
        "source_corpus_sha256": _sha256(corpus_path) if corpus_path else None,
        "case_count": len(cases),
        "source_document_count": len(documents),
        "mapped_case_count": len(mapping),
        "case_to_source_documents": mapping,
        "case_checks": case_checks,
        "review_summary": {
            "risk_case_count": risk_case_count,
            "risk_counts": dict(sorted(risk_counts.items())),
            "review_required": risk_case_count > 0,
        },
        "issues": sorted(set(issues) | ({"evidence_chunks_unavailable"} if corpus_path is None else set())),
        "real_service_acceptance": False,
        "review_notes": "Mapping is a deterministic proposal only. Review source content, scope, disclosure and business-date behavior before changing status to APPROVED.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=root / "synthetic_cases.jsonl")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, help="optional JSONL corpus for chunk position evidence")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    proposal = build_proposal(args.cases, args.manifest, args.corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: proposal[key] for key in ("status", "case_count", "source_document_count", "mapped_case_count", "issues", "real_service_acceptance")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
