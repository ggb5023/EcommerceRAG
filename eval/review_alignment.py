#!/usr/bin/env python3
"""Generate and validate a review-only checklist for M2 source alignment.

The checklist is separate from the proposal and the immutable evaluation set.
It records reviewer status without changing mappings or approving retrieval.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
from typing import Any

ALLOWED_STATUSES = {"pending", "approved", "needs_revision", "rejected"}
EVIDENCE_FIELDS = (
    "expected_doc_ids",
    "source_document_ids",
    "document_checks",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _evidence(check: dict[str, Any]) -> dict[str, Any]:
    document_checks = check.get("document_checks", [])
    return {
        "case_id": check.get("case_id"),
        "expected_doc_ids": check.get(
            "expected_doc_ids",
            [item.get("expected_doc_id") for item in document_checks],
        ),
        "source_document_ids": [item.get("source_document_id") for item in document_checks],
        "document_checks": document_checks,
    }


def build_checklist(proposal: dict[str, Any], proposal_path: Path) -> dict[str, Any]:
    checks = proposal.get("case_checks")
    if not isinstance(checks, list):
        raise ValueError("proposal.case_checks must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("case_id"), str):
            raise ValueError("proposal contains an invalid case check")
        case_id = check["case_id"]
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        rows.append({
            **_evidence(check),
            "review_status": "pending",
            "review_notes": "",
        })
    return {
        "review_version": "aligned-source-review-v1",
        "proposal_sha256": _sha256(proposal_path),
        "alignment_version": proposal.get("alignment_version"),
        "eval_set_version": proposal.get("eval_set_version"),
        "case_count": len(rows),
        "review_status_counts": {"pending": len(rows)},
        "status": "PENDING_REVIEW",
        "real_service_acceptance": False,
        "rows": rows,
        "notes": [
            "This checklist is an evidence record only; it never approves alignment.json.",
            "Review source content, tenant/shop scope, disclosure class and business-date behavior per case.",
        ],
    }


def validate_checklist(proposal: dict[str, Any], proposal_path: Path, review: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if review.get("review_version") != "aligned-source-review-v1":
        errors.append("review_version_invalid")
    if review.get("proposal_sha256") != _sha256(proposal_path):
        errors.append("proposal_sha256_mismatch")
    if review.get("real_service_acceptance") is not False:
        errors.append("real_service_acceptance_must_be_false")
    checks = proposal.get("case_checks") if isinstance(proposal.get("case_checks"), list) else []
    expected = {check.get("case_id"): _evidence(check) for check in checks if isinstance(check, dict)}
    rows = review.get("rows")
    if not isinstance(rows, list):
        errors.append("rows_not_list")
        rows = []
    row_ids = [row.get("case_id") for row in rows if isinstance(row, dict)]
    valid_row_ids = [case_id for case_id in row_ids if isinstance(case_id, str)]
    if len(valid_row_ids) != len(row_ids):
        errors.append("review_case_id_invalid")
    if len(valid_row_ids) != len(set(valid_row_ids)):
        errors.append("duplicate_review_case_id")
    if set(valid_row_ids) != set(expected):
        errors.append("review_case_id_set_mismatch")
    counts: collections.Counter[str] = collections.Counter()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"review_row_not_object:{index}")
            continue
        case_id = row.get("case_id")
        status = row.get("review_status")
        status_key = status if isinstance(status, str) else "<invalid>"
        counts[status_key] += 1
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            errors.append(f"review_status_invalid:{case_id}")
        if not isinstance(row.get("review_notes"), str):
            errors.append(f"review_notes_invalid:{case_id}")
        elif isinstance(status, str) and status in {"needs_revision", "rejected"} and not row["review_notes"].strip():
            errors.append(f"review_notes_missing:{case_id}")
        if isinstance(case_id, str) and case_id in expected:
            for field in EVIDENCE_FIELDS:
                if row.get(field) != expected[case_id].get(field):
                    errors.append(f"evidence_drift:{case_id}:{field}")
    unresolved = counts.get("pending", 0) + counts.get("needs_revision", 0) + counts.get("rejected", 0)
    status = "FAIL" if errors else "REVIEWED" if len(rows) == len(expected) and unresolved == 0 else "PENDING_REVIEW"
    return {
        "review_version": "aligned-source-review-v1",
        "status": status,
        "proposal_sha256": _sha256(proposal_path),
        "case_count": len(expected),
        "review_status_counts": dict(sorted(counts.items())),
        "unresolved_count": unresolved,
        "issues": sorted(set(errors)),
        "real_service_acceptance": False,
        "notes": [
            "REVIEWED means checklist rows are approved; a separate controlled action is still required to approve alignment.json.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--generate", action="store_true", help="write a new pending checklist")
    args = parser.parse_args()
    proposal = _load_object(args.proposal)
    if args.generate:
        if args.output.exists():
            raise SystemExit(f"refusing to overwrite existing review file: {args.output}")
        report = build_checklist(proposal, args.proposal)
    else:
        if args.review is None:
            raise SystemExit("--review is required unless --generate is set")
        report = validate_checklist(proposal, args.proposal, _load_object(args.review))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "case_count", "review_status_counts", "unresolved_count") if key in report}, ensure_ascii=False))
    return 0 if report["status"] in {"PENDING_REVIEW", "REVIEWED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
