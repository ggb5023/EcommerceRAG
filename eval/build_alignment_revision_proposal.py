#!/usr/bin/env python3
"""Build a review-only proposal for unresolved aligned-evidence rows.

The output is a decision aid. It never edits the immutable evaluation set,
the source corpus, the review checklist, or alignment.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        case_id = value.get("case_id") if isinstance(value, dict) else None
        if not isinstance(case_id, str) or case_id in rows:
            raise ValueError(f"invalid or duplicate case_id: {case_id!r}")
        rows[case_id] = value
    return rows


def _source_text(corpus: Path, document_id: str) -> str:
    for line_number, line in enumerate(corpus.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("document_id") == document_id:
            text = value.get("text") or value.get("content") or value.get("body")
            if isinstance(text, str):
                return text
            chunks = value.get("chunks")
            if isinstance(chunks, list):
                return "\n".join(
                    chunk.get("text", "") for chunk in chunks if isinstance(chunk, dict)
                )
            return ""
    raise ValueError(f"source document not found: {document_id}")


def build_proposal(cases_path: Path, review_path: Path, corpus_path: Path) -> dict[str, Any]:
    cases = _cases(cases_path)
    review = _load(review_path)
    if review.get("real_service_acceptance") is not False:
        raise ValueError("review real_service_acceptance must be false")
    if review.get("case_count") != len(cases):
        raise ValueError("review case_count does not match immutable input")
    proposal_sha = review.get("proposal_sha256")
    if not isinstance(proposal_sha, str) or len(proposal_sha) != 64:
        raise ValueError("review proposal_sha256 is missing or malformed")
    rows = review.get("rows") if isinstance(review, dict) else None
    if not isinstance(rows, list):
        raise TypeError("review.rows must be a list")
    proposals: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("review row must be an object")
        status = row.get("review_status")
        if status not in {"needs_revision", "rejected"}:
            continue
        case_id = row.get("case_id")
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"review case is absent from immutable input: {case_id}")
        checks = row.get("document_checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"unresolved case has no document evidence: {case_id}")
        source_ids = [check.get("source_document_id") for check in checks]
        source_texts = {source_id: _source_text(corpus_path, source_id) for source_id in source_ids}
        proposals.append(
            {
                "case_id": case_id,
                "review_status": status,
                "review_notes": row.get("review_notes", ""),
                "expected_doc_ids": case.get("expected_doc_ids", []),
                "current_expected_answer_points": case.get("expected_answer_points", []),
                "source_document_ids": source_ids,
                "source_text_sha256": {
                    source_id: hashlib.sha256(text.encode("utf-8")).hexdigest()
                    for source_id, text in source_texts.items()
                },
                "options": [
                    {
                        "option_id": "narrow_claim",
                        "action": "revise_expected_answer_points",
                        "description": "将答案改为明确的供应商声明语义，不声称所有批次绝对不含 BPA。",
                        "requires": "受控修改评测输入并产生新 eval_set_version/hash；不得覆盖当前 immutable 输入。",
                    },
                    {
                        "option_id": "add_test_evidence",
                        "action": "add_independent_source_evidence",
                        "description": "补充可追溯的批次检测或合规文件证据，再重新生成 mapping proposal。",
                        "requires": "来源文件、版本、SHA-256、source position 和授权/生效字段均需记录。",
                    },
                    {
                        "option_id": "keep_unresolved",
                        "action": "remain_needs_revision",
                        "description": "保留当前未决状态，继续阻断 alignment 和普通检索指标。",
                        "requires": "无需修改输入；M2 aligned retrieval 保持 NOT_RUN/null。",
                    },
                ],
            }
        )
    review_counts = review.get("review_status_counts")
    if not isinstance(review_counts, dict):
        raise ValueError("review_status_counts is missing")
    expected_unresolved = sum(
        int(review_counts.get(status, 0) or 0)
        for status in ("needs_revision", "rejected")
    )
    if expected_unresolved != len(proposals):
        raise ValueError("review status counts disagree with unresolved rows")
    return {
        "proposal_version": "aligned-evidence-revision-v1",
        "status": "PENDING_REVIEW" if proposals else "NO_UNRESOLVED_ROWS",
        "case_count": len(cases),
        "unresolved_count": len(proposals),
        "cases_sha256": _sha256(cases_path),
        "review_sha256": _sha256(review_path),
        "source_corpus_sha256": _sha256(corpus_path),
        "real_service_acceptance": False,
        "rows": proposals,
        "notes": [
            "This is a review aid only; it never edits immutable inputs or approves alignment.json.",
            "A revised evaluation set must receive a new version and hash instead of replacing the current set.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_proposal(args.cases, args.review, args.corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "case_count", "unresolved_count", "real_service_acceptance")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
