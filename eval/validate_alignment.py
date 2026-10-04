#!/usr/bin/env python3
"""Validate an aligned evaluation mapping without approving or measuring it."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from alignment_policy import assess_document, ALLOWED_DISCLOSURE_CLASSES

ALLOWED_ALIGNMENT_STATUSES = {"PENDING_REVIEW", "APPROVED", "REJECTED"}
REQUIRED_CASE_COUNT = 60


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        rows: list[dict[str, Any]] = []
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError(f"line {line_number} is not an object")
            rows.append(value)
        return rows, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        return [], f"jsonl_input_invalid:{type(error).__name__}"


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("root must be an object")
        return value, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        return {}, f"json_input_invalid:{type(error).__name__}"


def _valid_date(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_corpus(corpus: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    documents: dict[str, dict[str, Any]] = {}
    chunk_ids: set[str] = set()
    for index, document in enumerate(corpus, 1):
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            errors.append(f"corpus_document_id_invalid:{index}")
            continue
        if document_id in documents:
            errors.append(f"duplicate_corpus_document_id:{document_id}")
            continue
        if not all(isinstance(document.get(field), str) and document[field] for field in ("tenant_id", "shop_id")):
            errors.append(f"corpus_scope_invalid:{document_id}")
        chunks = document.get("chunks")
        if not isinstance(chunks, list):
            errors.append(f"corpus_chunks_invalid:{document_id}")
            chunks = []
        for chunk_number, chunk in enumerate(chunks, 1):
            if not isinstance(chunk, dict):
                errors.append(f"corpus_chunk_invalid:{document_id}:{chunk_number}")
                continue
            chunk_id = chunk.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                errors.append(f"corpus_chunk_id_invalid:{document_id}:{chunk_number}")
            elif chunk_id in chunk_ids:
                errors.append(f"duplicate_corpus_chunk_id:{chunk_id}")
            else:
                chunk_ids.add(chunk_id)
            if chunk.get("tenant_id") != document.get("tenant_id") or chunk.get("shop_id") != document.get("shop_id"):
                errors.append(f"corpus_chunk_scope_mismatch:{document_id}:{chunk_number}")
            if not _valid_date(chunk.get("effective_from")) or not _valid_date(chunk.get("effective_to")):
                errors.append(f"corpus_chunk_date_invalid:{document_id}:{chunk_number}")
            if (
                chunk.get("effective_from")
                and chunk.get("effective_to")
                and date.fromisoformat(chunk["effective_from"]) >= date.fromisoformat(chunk["effective_to"])
            ):
                errors.append(f"corpus_chunk_date_range_invalid:{document_id}:{chunk_number}")
            if chunk.get("disclosure_class") not in ALLOWED_DISCLOSURE_CLASSES:
                errors.append(f"corpus_chunk_disclosure_class_invalid:{document_id}:{chunk_number}")
        documents[document_id] = document
    return documents, errors


def _validate_mapping(
    cases: list[dict[str, Any]],
    corpus: dict[str, dict[str, Any]],
    alignment: dict[str, Any],
) -> tuple[list[str], collections.Counter, int, collections.Counter]:
    errors: list[str] = []
    statuses: collections.Counter = collections.Counter()
    policy_statuses: collections.Counter = collections.Counter()
    mapping = alignment.get("case_to_source_documents", {})
    if not isinstance(mapping, dict):
        return ["alignment_case_to_source_documents_not_object"], statuses, 0, policy_statuses
    case_by_id = {case.get("case_id"): case for case in cases}
    unknown_cases = sorted(set(mapping) - set(case_by_id))
    errors.extend(f"unknown_alignment_case_id:{case_id}" for case_id in unknown_cases)
    complete = 0
    for case in cases:
        case_id = case.get("case_id")
        case_mapping = mapping.get(case_id)
        if not isinstance(case_mapping, dict) or not case_mapping:
            statuses["missing"] += 1
            continue
        expected_ids = case.get("expected_doc_ids", [])
        if set(case_mapping) != set(expected_ids):
            errors.append(f"{case_id}:alignment_keys_do_not_match_expected_doc_ids")
            statuses["invalid"] += 1
            continue
        source_ids = list(case_mapping.values())
        if not all(isinstance(source_id, str) and source_id for source_id in source_ids):
            errors.append(f"{case_id}:alignment_source_id_invalid")
            statuses["invalid"] += 1
            continue
        if len(set(source_ids)) != len(source_ids):
            errors.append(f"{case_id}:source_document_alias")
            statuses["invalid"] += 1
            continue
        missing = sorted(source_id for source_id in source_ids if source_id not in corpus)
        if missing:
            errors.append(f"{case_id}:missing_source_document:{','.join(missing)}")
            statuses["invalid"] += 1
            continue
        authorization = case.get("authorization", {})
        scope_mismatch = sorted(
            source_id
            for source_id in source_ids
            if corpus[source_id].get("tenant_id") != authorization.get("tenant_id")
            or corpus[source_id].get("shop_id") != authorization.get("shop_id")
        )
        if scope_mismatch:
            errors.append(f"{case_id}:source_document_scope_mismatch:{','.join(scope_mismatch)}")
            statuses["invalid"] += 1
            continue
        for source_id in source_ids:
            policy = assess_document(corpus[source_id], case.get("business_date"), authorization)
            policy_statuses[policy["status"]] += 1
            if policy["status"] in {"INVALID_POLICY", "INVALID_BUSINESS_DATE", "SCOPE_MISMATCH"}:
                errors.append(f"{case_id}:source_policy_invalid:{source_id}:{policy['status']}")
        statuses["complete"] += 1
        complete += 1
    return errors, statuses, complete, policy_statuses


def validate(cases_path: Path, metadata_path: Path, corpus_path: Path, alignment_path: Path) -> dict[str, Any]:
    cases, cases_error = _load_jsonl(cases_path)
    corpus_rows, corpus_error = _load_jsonl(corpus_path)
    metadata, metadata_error = _load_json(metadata_path)
    alignment, alignment_error = _load_json(alignment_path)
    issues = [error for error in (cases_error, corpus_error, metadata_error, alignment_error) if error]
    corpus, corpus_issues = _validate_corpus(corpus_rows)
    issues.extend(corpus_issues)

    actual_cases_sha = _sha256(cases_path)
    if len(cases) != REQUIRED_CASE_COUNT:
        issues.append(f"case_count:{len(cases)}!={REQUIRED_CASE_COUNT}")
    if metadata.get("case_count") != len(cases):
        issues.append("metadata_case_count_mismatch")
    if metadata.get("sha256") != actual_cases_sha:
        issues.append("cases_sha256_mismatch")
    case_ids = [case.get("case_id") for case in cases]
    duplicate_case_ids = sorted(case_id for case_id, count in collections.Counter(case_ids).items() if count > 1)
    issues.extend(f"duplicate_case_id:{case_id}" for case_id in duplicate_case_ids)

    alignment_status = alignment.get("status")
    if alignment_status not in ALLOWED_ALIGNMENT_STATUSES:
        issues.append(f"alignment_status_invalid:{alignment_status or 'missing'}")
    if alignment.get("real_service_acceptance") is not False:
        issues.append("real_service_acceptance_must_be_false")
    mapping = alignment.get("case_to_source_documents")
    if isinstance(mapping, dict) and not mapping:
        issues.append("alignment_mapping_empty")
    if alignment_status == "PENDING_REVIEW":
        issues.append("alignment_pending_review")

    mapping_errors, mapping_counts, complete_count, policy_statuses = _validate_mapping(cases, corpus, alignment)
    issues.extend(mapping_errors)
    structural_prefixes = (
        "jsonl_input_invalid:",
        "json_input_invalid:",
        "corpus_",
        "duplicate_",
        "case_count:",
        "metadata_",
        "cases_sha256_",
        "alignment_status_invalid:",
        "real_service_acceptance_",
        "alignment_case_to_source_documents_not_object",
    )
    structural_errors = [issue for issue in issues if issue.startswith(structural_prefixes)]
    if structural_errors:
        status = "FAIL"
    elif alignment_status == "APPROVED" and complete_count == len(cases) and not issues:
        status = "PASS"
    else:
        status = "PENDING_REVIEW"
    return {
        "report_version": "aligned-mapping-validation-v1",
        "status": status,
        "eval_set_version": metadata.get("eval_set_version"),
        "cases_sha256": actual_cases_sha,
        "metadata_sha256": metadata.get("sha256"),
        "corpus_sha256": _sha256(corpus_path),
        "alignment_sha256": _sha256(alignment_path),
        "case_count": len(cases),
        "corpus_document_count": len(corpus),
        "mapped_case_count": mapping_counts.get("complete", 0),
        "missing_mapping_case_count": mapping_counts.get("missing", 0),
        "invalid_mapping_case_count": mapping_counts.get("invalid", 0),
        "policy_status_counts": dict(sorted(policy_statuses.items())),
        "issues": sorted(set(issues)),
        "real_service_acceptance": False,
        "notes": [
            "Read-only structural and authorization-scope validation; this tool never approves mappings.",
            "Retrieval metrics require a separate approved alignment and run_aligned_retrieval.py.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=root / "synthetic_cases.jsonl")
    parser.add_argument("--metadata", type=Path, default=root / "synthetic_cases.metadata.json")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned/documents.jsonl"),
    )
    parser.add_argument("--alignment", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    alignment_path = args.alignment or args.corpus.with_name("alignment.json")
    report = validate(args.cases, args.metadata, args.corpus, alignment_path)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"PASS", "PENDING_REVIEW"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
