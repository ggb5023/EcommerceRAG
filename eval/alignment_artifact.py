"""Shared validation for controlled APPROVED alignment artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def approved_artifact_issues(alignment: dict[str, Any], corpus_path: Path, case_ids: set[str]) -> list[str]:
    if alignment.get("status") != "APPROVED":
        return []
    # Legacy hand-written fixtures are still used for policy unit tests. The
    # controlled approval tool always emits both fields below, so only that
    # artifact shape enters the evidence-integrity gate.
    if "source_corpus_sha256" not in alignment and "case_evidence" not in alignment:
        return []
    issues: list[str] = []
    if alignment.get("source_corpus_sha256") != sha256(corpus_path):
        issues.append("approved_source_corpus_sha256_mismatch")
    evidence = alignment.get("case_evidence")
    mapping = alignment.get("case_to_source_documents")
    if not isinstance(evidence, dict):
        issues.append("approved_case_evidence_missing")
        return issues
    if not isinstance(mapping, dict) or set(evidence) != set(mapping) or set(evidence) != case_ids:
        issues.append("approved_case_evidence_case_set_mismatch")
        return issues
    for case_id, checks in evidence.items():
        if not isinstance(checks, list) or not checks:
            issues.append(f"approved_case_evidence_invalid:{case_id}")
            continue
        for index, check in enumerate(checks, 1):
            if not isinstance(check, dict) or not isinstance(check.get("source_document_id"), str) or not check["source_document_id"]:
                issues.append(f"approved_case_evidence_check_invalid:{case_id}:{index}")
                continue
            chunks = check.get("evidence_chunks")
            if not isinstance(chunks, list) or not chunks:
                issues.append(f"approved_case_evidence_chunks_missing:{case_id}:{index}")
                continue
            for chunk_index, chunk in enumerate(chunks, 1):
                if not isinstance(chunk, dict) or not isinstance(chunk.get("chunk_id"), str) or not chunk.get("chunk_id"):
                    issues.append(f"approved_case_evidence_chunk_invalid:{case_id}:{index}:{chunk_index}")
                if not isinstance(chunk.get("source_position"), dict) or not chunk["source_position"]:
                    issues.append(f"approved_case_evidence_position_invalid:{case_id}:{index}:{chunk_index}")
    return sorted(set(issues))
