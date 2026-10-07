"""Validate a reviewed, point-to-source evidence sidecar.

The immutable evaluation JSONL stores the expected answer points.  This module
keeps the separate human/engineering review that binds each point to approved
source chunks.  It never copies source text into reports and never changes the
evaluation input or the approved alignment artifact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ALLOWED_REVIEW_STATUSES = {"pending", "approved", "needs_revision", "rejected"}
ALLOWED_ARTIFACT_STATUSES = {"PENDING_REVIEW", "APPROVED"}
APPROVED_BASIS = "semantic_source_review"


def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)


def _approved_chunk_ids(case_id: str, alignment: Mapping[str, Any]) -> set[str]:
    chunks: set[str] = set()
    checks = alignment.get("case_evidence", {}).get(case_id, [])
    if not isinstance(checks, list):
        return chunks
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        evidence_chunks = check.get("evidence_chunks", [])
        if not isinstance(evidence_chunks, list):
            continue
        for chunk in evidence_chunks:
            if isinstance(chunk, Mapping) and isinstance(chunk.get("chunk_id"), str):
                chunks.add(chunk["chunk_id"])
    return chunks


def _corpus_chunk_index(corpus: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for document in corpus:
        document_id = document.get("document_id")
        if not isinstance(document_id, str):
            continue
        chunks = document.get("chunks", [])
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            chunk_id = chunk.get("chunk_id")
            content = chunk.get("content")
            if isinstance(chunk_id, str) and isinstance(content, str):
                index[chunk_id] = (document_id, content)
    return index


def _case_points(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for case in cases:
        case_id = case.get("case_id")
        points = case.get("expected_answer_points")
        if isinstance(case_id, str) and isinstance(points, list):
            result[case_id] = len(points)
    return result


def validate_review(
    review_path: Path,
    cases_path: Path,
    corpus_path: Path,
    alignment_path: Path,
    cases: Sequence[Mapping[str, Any]],
    corpus: Sequence[Mapping[str, Any]],
    alignment: Mapping[str, Any],
) -> tuple[dict[str, list[bool]], dict[str, Any]]:
    """Return per-case approved support flags and a metadata-only summary."""

    summary: dict[str, Any] = {
        "review_version": "answer-point-evidence-review-v1",
        "status": "NOT_RUN",
        "review_sha256": sha256(review_path),
        "cases_sha256": sha256(cases_path),
        "alignment_sha256": sha256(alignment_path),
        "source_corpus_sha256": sha256(corpus_path),
        "approved_point_count": 0,
        "unresolved_point_count": 0,
        "point_count": sum(_case_points(cases).values()),
        "approved_support_chunk_ids": {},
        "issues": [],
    }
    try:
        review = load_json(review_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        summary["issues"] = [f"review_invalid:{type(error).__name__}"]
        return {}, summary

    issues: list[str] = []
    if not isinstance(review, Mapping):
        issues.append("review_root_not_object")
        summary["issues"] = issues
        return {}, summary
    if review.get("review_version") != "answer-point-evidence-review-v1":
        issues.append("review_version_invalid")
    if review.get("real_service_acceptance") is not False:
        issues.append("review_real_service_acceptance_not_false")
    if review.get("cases_sha256") != summary["cases_sha256"]:
        issues.append("review_cases_sha256_mismatch")
    if review.get("alignment_sha256") != summary["alignment_sha256"]:
        issues.append("review_alignment_sha256_mismatch")
    if review.get("source_corpus_sha256") != summary["source_corpus_sha256"]:
        issues.append("review_source_corpus_sha256_mismatch")
    if alignment.get("status") != "APPROVED":
        issues.append("alignment_not_approved")

    expected_points = _case_points(cases)
    rows = review.get("rows")
    if not isinstance(rows, list):
        issues.append("review_rows_not_array")
        summary["issues"] = issues
        return {}, summary

    corpus_chunks = _corpus_chunk_index(corpus)
    approved_chunks_by_case = {
        case_id: _approved_chunk_ids(case_id, alignment) for case_id in expected_points
    }
    flags: dict[str, list[bool]] = {case_id: [False] * count for case_id, count in expected_points.items()}
    support_chunks = {
        case_id: [[] for _ in range(count)] for case_id, count in expected_points.items()
    }
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            issues.append("review_row_not_object")
            continue
        case_id = row.get("case_id")
        point_index = row.get("point_index")
        key = (case_id, point_index) if isinstance(case_id, str) and isinstance(point_index, int) else None
        if key is None:
            issues.append("review_row_identity_invalid")
            continue
        if key in seen:
            issues.append(f"duplicate_review_row:{case_id}:{point_index}")
        seen.add(key)
        if case_id not in expected_points:
            issues.append(f"unknown_case_id:{case_id}")
            continue
        if point_index < 0 or point_index >= expected_points[case_id]:
            issues.append(f"point_index_out_of_range:{case_id}:{point_index}")
            continue
        status = row.get("review_status")
        if status not in ALLOWED_REVIEW_STATUSES:
            issues.append(f"review_status_invalid:{case_id}:{point_index}")
            continue
        chunk_ids = row.get("support_chunk_ids")
        if not isinstance(chunk_ids, list) or not chunk_ids or not all(isinstance(value, str) and value for value in chunk_ids):
            issues.append(f"support_chunk_ids_invalid:{case_id}:{point_index}")
            continue
        if len(set(chunk_ids)) != len(chunk_ids):
            issues.append(f"duplicate_support_chunk_id:{case_id}:{point_index}")
        allowed = approved_chunks_by_case[case_id]
        for chunk_id in chunk_ids:
            if chunk_id not in allowed:
                issues.append(f"support_chunk_not_in_approved_alignment:{case_id}:{point_index}:{chunk_id}")
            source = corpus_chunks.get(chunk_id)
            if source is None or not source[1].strip():
                issues.append(f"support_chunk_missing_from_corpus:{case_id}:{point_index}:{chunk_id}")
        basis = row.get("support_basis")
        if status == "approved":
            if basis != APPROVED_BASIS:
                issues.append(f"approved_basis_invalid:{case_id}:{point_index}")
            flags[case_id][point_index] = True
            support_chunks[case_id][point_index] = list(chunk_ids)
            summary["approved_point_count"] += 1
        else:
            if not isinstance(row.get("review_notes_code"), str) or not row["review_notes_code"]:
                issues.append(f"unresolved_review_note_missing:{case_id}:{point_index}")
            summary["unresolved_point_count"] += 1

    expected_rows = sum(expected_points.values())
    if len(rows) != expected_rows:
        issues.append(f"review_row_count:{len(rows)}")
    if seen != {(case_id, index) for case_id, count in expected_points.items() for index in range(count)}:
        issues.append("review_point_set_mismatch")

    declared_status = review.get("status")
    expected_status = "APPROVED" if not issues and summary["unresolved_point_count"] == 0 else "PENDING_REVIEW"
    if declared_status != expected_status:
        issues.append("review_status_summary_mismatch")
    summary["status"] = declared_status if declared_status in ALLOWED_ARTIFACT_STATUSES else "FAIL"
    summary["issues"] = sorted(set(issues))
    if summary["issues"]:
        return {}, summary
    summary["approved_support_chunk_ids"] = support_chunks
    return flags, summary
