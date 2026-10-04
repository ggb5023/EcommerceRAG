#!/usr/bin/env python3
"""Controlled approval of a reviewed aligned source proposal.

Approval is an explicit operator action. This tool refuses unresolved review
rows, validates evidence hashes and writes a new artifact atomically without
modifying the proposal, review, corpus, or immutable evaluation input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def build_approved_alignment(proposal: dict[str, Any], proposal_path: Path, review: dict[str, Any], review_path: Path, corpus_path: Path) -> dict[str, Any]:
    if proposal.get("status") != "PENDING_REVIEW":
        raise ValueError("proposal must be PENDING_REVIEW")
    if proposal.get("real_service_acceptance") is not False:
        raise ValueError("proposal real_service_acceptance must be false")
    if review.get("proposal_sha256") != _sha256(proposal_path):
        raise ValueError("review proposal_sha256 does not match proposal")
    if review.get("real_service_acceptance") is not False:
        raise ValueError("review real_service_acceptance must be false")
    rows = review.get("rows")
    checks = proposal.get("case_checks")
    if not isinstance(rows, list) or not isinstance(checks, list):
        raise TypeError("proposal case_checks and review rows must be lists")
    if review.get("case_count") != len(checks) or len(rows) != len(checks):
        raise ValueError("proposal/review case counts disagree")
    by_review = {row.get("case_id"): row for row in rows if isinstance(row, dict)}
    if len(by_review) != len(rows) or set(by_review) != {check.get("case_id") for check in checks}:
        raise ValueError("proposal and review case IDs must match exactly")
    counts = review.get("review_status_counts")
    if counts != {"approved": len(rows)}:
        raise ValueError("all mapping rows must be approved")
    mapping = proposal.get("case_to_source_documents")
    if not isinstance(mapping, dict) or set(mapping) != set(by_review):
        raise ValueError("proposal mapping must cover every reviewed case")
    for check in checks:
        case_id = check.get("case_id")
        row = by_review[case_id]
        if row.get("review_status") != "approved" or row.get("review_notes", "").strip():
            raise ValueError(f"review row is not cleanly approved: {case_id}")
        expected = {
            "case_id": case_id,
            "expected_doc_ids": check.get("expected_doc_ids"),
            "source_document_ids": [item.get("source_document_id") for item in check.get("document_checks", [])],
            "document_checks": check.get("document_checks"),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                raise ValueError(f"review evidence drift: {case_id}:{key}")
    return {
        "alignment_version": proposal.get("alignment_version"),
        "status": "APPROVED",
        "eval_set_version": proposal.get("eval_set_version"),
        "cases_sha256": proposal.get("cases_sha256"),
        "source_manifest_sha256": proposal.get("source_manifest_sha256"),
        "source_corpus_sha256": _sha256(corpus_path),
        "proposal_sha256": _sha256(proposal_path),
        "review_sha256": _sha256(review_path),
        "case_count": len(checks),
        "case_to_source_documents": mapping,
        "case_evidence": {
            check["case_id"]: check.get("document_checks", []) for check in checks
        },
        "real_service_acceptance": False,
        "approval_notes": "Explicit controlled approval from a complete source evidence review; this remains a synthetic local evaluation artifact.",
    }


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approve", action="store_true", help="explicitly write the approved artifact")
    args = parser.parse_args()
    proposal = _load(args.proposal)
    review = _load(args.review)
    if args.output.exists() and args.approve:
        raise SystemExit(f"refusing to overwrite existing alignment: {args.output}")
    approved = build_approved_alignment(proposal, args.proposal, review, args.review, args.corpus)
    if args.approve:
        _write_atomic(args.output, approved)
    print(json.dumps({"status": approved["status"], "case_count": approved["case_count"], "written": args.approve}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
