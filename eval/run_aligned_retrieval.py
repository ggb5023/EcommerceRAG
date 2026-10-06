#!/usr/bin/env python3
"""Run read-only retrieval metrics only for explicitly approved alignments."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path

from alignment_artifact import approved_artifact_issues
from alignment_policy import assess_document


class InputIntegrityError(ValueError):
    """Raised when an evaluator input cannot be parsed safely."""


def tokens(value: str) -> set[str]:
    value = value.lower()
    result = set(re.findall(r"[a-z0-9_]+", value))
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(
            run[i : i + size]
            for size in (2, 3, 4)
            for i in range(len(run))
            if i + size <= len(run)
        )
    return result


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys instead of silently accepting the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputIntegrityError(f"{label} is not valid UTF-8: {error}") from error
    except OSError as error:
        raise InputIntegrityError(f"{label} cannot be read: {error}") from error


def _load_lines(path: Path, label: str) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(_read_utf8(path, label).splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except InputIntegrityError as error:
            raise InputIntegrityError(f"{label} line {line_number}: {error}") from error
        except json.JSONDecodeError as error:
            raise InputIntegrityError(f"{label} line {line_number} invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise InputIntegrityError(f"{label} line {line_number} must be a JSON object")
        rows.append(value)
    return rows


def _load_object(path: Path, label: str) -> dict:
    text = _read_utf8(path, label)
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except InputIntegrityError:
        raise
    except json.JSONDecodeError as error:
        raise InputIntegrityError(f"{label} invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise InputIntegrityError(f"{label} must be a JSON object")
    return value


def _validate_case_shapes(cases: list[dict]) -> None:
    issues: list[str] = []
    for number, case in enumerate(cases, 1):
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            issues.append(f"case {number} case_id must be a non-empty string")
        if not isinstance(case.get("query"), str):
            issues.append(f"case {number} query must be a string")
        expected_ids = case.get("expected_doc_ids")
        if not isinstance(expected_ids, list) or not expected_ids or not all(
            isinstance(value, str) and value for value in expected_ids
        ):
            issues.append(f"case {number} expected_doc_ids must be a non-empty string list")
        answer_points = case.get("expected_answer_points")
        if not isinstance(answer_points, list) or not answer_points or not all(
            isinstance(value, str) and value for value in answer_points
        ):
            issues.append(f"case {number} expected_answer_points must be a non-empty string list")
        authorization = case.get("authorization")
        if not isinstance(authorization, dict) or not all(
            isinstance(authorization.get(field), str) and authorization[field]
            for field in ("tenant_id", "shop_id", "role")
        ):
            issues.append(f"case {number} authorization is incomplete")
        business_date = case.get("business_date")
        if not isinstance(business_date, str):
            issues.append(f"case {number} business_date must be a string")
        else:
            try:
                date.fromisoformat(business_date)
            except ValueError:
                issues.append(f"case {number} business_date is invalid")
    if issues:
        raise InputIntegrityError("; ".join(issues[:8]))


def _validate_corpus_shapes(corpus: list[dict]) -> None:
    issues: list[str] = []
    for number, document in enumerate(corpus, 1):
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            issues.append(f"corpus document {number} document_id must be a non-empty string")
        if not all(isinstance(document.get(field), str) and document[field] for field in ("tenant_id", "shop_id")):
            issues.append(f"corpus document {number} scope is incomplete")
        chunks = document.get("chunks")
        if not isinstance(chunks, list):
            issues.append(f"corpus document {number} chunks must be a list")
            continue
        for chunk_number, chunk in enumerate(chunks, 1):
            if not isinstance(chunk, dict):
                issues.append(f"corpus document {number} chunk {chunk_number} must be an object")
                continue
            if not isinstance(chunk.get("chunk_id"), str) or not chunk["chunk_id"]:
                issues.append(f"corpus document {number} chunk {chunk_number} chunk_id is invalid")
            if not isinstance(chunk.get("content"), str):
                issues.append(f"corpus document {number} chunk {chunk_number} content must be a string")
            if chunk.get("tenant_id") != document.get("tenant_id") or chunk.get("shop_id") != document.get("shop_id"):
                issues.append(f"corpus document {number} chunk {chunk_number} scope mismatch")
    if issues:
        raise InputIntegrityError("; ".join(issues[:8]))


def _not_run_report(cases_path: Path, corpus_path: Path, alignment_path: Path,
                    *, issues: list[str], case_count: int = 0,
                    eval_set_version: str = "synthetic-m2-v1") -> dict:
    return {
        "report_version": "aligned-retrieval-v2",
        "eval_set_version": eval_set_version,
        "input_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest() if cases_path.is_file() else None,
        "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest() if corpus_path.is_file() else None,
        "alignment_sha256": hashlib.sha256(alignment_path.read_bytes()).hexdigest() if alignment_path.is_file() else None,
        "pipeline_version": "local-source-alignment-v1",
        "model_version": "deterministic-keyword-v1",
        "real_service_acceptance": False,
        "status": "NOT_RUN",
        "metrics": {"recall_at_5": None, "mrr": None, "ndcg_at_5": None},
        "case_count": case_count,
        "measured_case_count": 0,
        "status_counts": {"NOT_RUN": case_count} if case_count else {},
        "issues": issues,
        "notes": ["No metrics are emitted until input and explicit alignment checks pass."],
        "case_results": [],
    }


def _in_date(chunk: dict, business_date: str) -> bool:
    current = date.fromisoformat(business_date)
    start = date.fromisoformat(chunk["effective_from"]) if chunk.get("effective_from") else None
    end = date.fromisoformat(chunk["effective_to"]) if chunk.get("effective_to") else None
    return not ((start and current < start) or (end and current >= end))


def _dcg(relevances: list[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevances))


def _metrics(rows: list[dict]) -> dict[str, float | None]:
    measured = [row for row in rows if row["status"] == "MEASURED"]
    if not measured:
        return {"recall_at_5": None, "mrr": None, "ndcg_at_5": None}
    return {
        "recall_at_5": sum(row["hit"] for row in measured) / len(measured),
        "mrr": sum(row["reciprocal_rank"] for row in measured) / len(measured),
        "ndcg_at_5": sum(row["ndcg_at_5"] for row in measured) / len(measured),
    }


def _policy_refusal_expected(case: dict) -> bool:
    tags = set(case.get("tags", []))
    if tags & {"unauthorized", "unanswerable", "revoked", "expired"}:
        return True
    # Some legacy synthetic cases encode a refusal in the expected answer
    # point but predate the explicit policy tag.  Keep this narrow and
    # deterministic so ordinary negative knowledge cases are not swallowed.
    points = " ".join(point for point in case.get("expected_answer_points", []) if isinstance(point, str))
    return any(marker in points for marker in ("拒绝", "撤销", "撤权", "不能继续", "无法", "不应"))


def _acceptable_policy_refusal(case: dict, status: str) -> bool:
    """Only a refusal explicitly expected by the case can complete a run."""
    return status in {
        "ACCESS_DENIED",
        "UNCLASSIFIED_BLOCKED",
        "EXPIRED_OR_REVOKED",
        "NOT_YET_EFFECTIVE",
    } and _policy_refusal_expected(case)


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent
    parser.add_argument("--cases", type=Path, default=root / "synthetic_cases.jsonl")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned/documents.jsonl"),
    )
    parser.add_argument("--alignment", type=Path)
    parser.add_argument("--metadata", type=Path, default=root / "synthetic_cases.metadata.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    alignment_path = args.alignment or args.corpus.with_name("alignment.json")
    try:
        metadata = _load_object(args.metadata, "metadata")
        eval_set_version = metadata.get("eval_set_version", "synthetic-m2-v1")
        if not isinstance(eval_set_version, str) or not eval_set_version:
            raise InputIntegrityError("metadata eval_set_version must be a non-empty string")
        cases = _load_lines(args.cases, "cases")
        corpus = _load_lines(args.corpus, "corpus")
        _validate_case_shapes(cases)
        _validate_corpus_shapes(corpus)
        alignment = _load_object(alignment_path, "alignment") if alignment_path.exists() else {}
    except InputIntegrityError as error:
        print(f"FAIL input_integrity: {error}")
        return 1
    corpus_ids = [doc.get("document_id") for doc in corpus if isinstance(doc, dict)]
    duplicate_corpus_ids = sorted({doc_id for doc_id in corpus_ids if doc_id and corpus_ids.count(doc_id) > 1})
    corpus_by_id = {
        doc["document_id"]: doc for doc in corpus
        if isinstance(doc, dict) and isinstance(doc.get("document_id"), str)
    }
    mapping = alignment.get("case_to_source_documents", {})
    approved = alignment.get("status") == "APPROVED" and alignment.get("real_service_acceptance") is False
    rows: list[dict] = []
    issues: list[str] = []
    refusal_counts: collections.Counter = collections.Counter()
    if duplicate_corpus_ids:
        issues.append("duplicate_corpus_document_id:" + ",".join(duplicate_corpus_ids))
    if not isinstance(mapping, dict):
        issues.append("alignment_case_to_source_documents_not_object")
        mapping = {}
    if approved:
        approval_issues = approved_artifact_issues(alignment, args.corpus, {case.get("case_id") for case in cases})
        issues.extend(approval_issues)
        if approval_issues:
            approved = False
            input_invalid = True
    input_invalid = bool(duplicate_corpus_ids or "alignment_case_to_source_documents_not_object" in issues)
    if not approved:
        status = alignment.get("status") if isinstance(alignment, dict) else None
        if status == "PENDING_REVIEW":
            issues.append("alignment_pending_review")
        else:
            issues.append(f"alignment_not_approved:{status or 'missing'}")
    if not mapping:
        issues.append("alignment_mapping_empty")
    for case in cases:
        case_id = case["case_id"]
        if input_invalid:
            rows.append({"case_id": case_id, "status": "NOT_RUN", "reason": "input_integrity"})
            continue
        case_mapping = mapping.get(case_id, {})
        if not approved:
            rows.append({"case_id": case_id, "status": "NOT_RUN", "reason": "alignment_pending_review"})
            continue
        if not isinstance(case_mapping, dict) or not case_mapping:
            rows.append({"case_id": case_id, "status": "NOT_RUN", "reason": "missing_alignment"})
            continue
        expected_ids = set(case.get("expected_doc_ids", []))
        if set(case_mapping) != expected_ids or not all(
            isinstance(source_id, str) and source_id for source_id in case_mapping.values()
        ):
            issues.append(f"{case_id}: alignment keys must exactly match expected_doc_ids")
            rows.append({"case_id": case_id, "status": "ALIGNMENT_INVALID", "reason": "expected_doc_id_drift"})
            continue
        source_ids = list(case_mapping.values())
        if len(set(source_ids)) != len(source_ids):
            issues.append(f"{case_id}: multiple expected IDs map to one source document")
            rows.append({"case_id": case_id, "status": "ALIGNMENT_INVALID", "reason": "source_document_alias"})
            continue
        missing = [doc_id for doc_id in source_ids if doc_id not in corpus_by_id]
        if missing:
            issues.append(f"{case_id}: missing source document(s): {','.join(missing)}")
            rows.append({"case_id": case_id, "status": "ALIGNMENT_INVALID", "missing": missing})
            continue
        query_tokens = tokens(case["query"])
        allowed = case["authorization"]
        scope_mismatch = [
            doc_id for doc_id in source_ids
            if corpus_by_id[doc_id].get("tenant_id") != allowed.get("tenant_id")
            or corpus_by_id[doc_id].get("shop_id") != allowed.get("shop_id")
        ]
        if scope_mismatch:
            issues.append(f"{case_id}: source document scope mismatch:{','.join(scope_mismatch)}")
            rows.append({"case_id": case_id, "status": "ALIGNMENT_INVALID",
                         "reason": "document_scope_mismatch", "document_ids": scope_mismatch})
            continue
        policy_rows = [
            {
                "document_id": doc_id,
                "policy": assess_document(corpus_by_id[doc_id], case.get("business_date"), allowed),
            }
            for doc_id in source_ids
        ]
        blocked = [
            item for item in policy_rows
            if item["policy"]["status"] != "ELIGIBLE"
        ]
        if blocked:
            blocked_statuses = [item["policy"]["status"] for item in blocked]
            primary_status = blocked_statuses[0] if len(set(blocked_statuses)) == 1 else "POLICY_BLOCKED"
            expected_refusal = _policy_refusal_expected(case)
            refusal_match = _acceptable_policy_refusal(case, primary_status)
            refusal_counts[primary_status] += 1
            refusal_counts["expected_refusal" if expected_refusal else "unexpected_refusal"] += 1
            refusal_counts["matched" if refusal_match else "unmatched"] += 1
            if not refusal_match:
                issues.append(f"{case_id}:unexpected_policy_refusal:{primary_status}")
            rows.append(
                {
                    "case_id": case_id,
                    "status": primary_status,
                    "policy_status": primary_status,
                    "blocked_document_ids": [item["document_id"] for item in blocked],
                    "policy_risk_reasons": sorted({
                        reason
                        for item in blocked
                        for reason in item["policy"].get("risk_reasons", [])
                    }),
                    "expected_refusal": expected_refusal,
                    "refusal_match": refusal_match,
                }
            )
            continue
        ranked_by_document: dict[str, tuple[int, dict]] = {}
        for doc_id in source_ids:
            document = corpus_by_id[doc_id]
            for chunk in document.get("chunks", []):
                if chunk["tenant_id"] != allowed["tenant_id"] or chunk["shop_id"] != allowed["shop_id"]:
                    continue
                if not _in_date(chunk, case["business_date"]):
                    continue
                if chunk["disclosure_class"] != "external_allowed" and allowed["role"] not in {"admin", "owner"}:
                    continue
                score = len(query_tokens & tokens(chunk["content"]))
                if score:
                    current = ranked_by_document.get(doc_id)
                    candidate = (score, chunk)
                    if current is None or (-score, chunk["chunk_id"]) < (-current[0], current[1]["chunk_id"]):
                        ranked_by_document[doc_id] = candidate
        ranked = [(score, doc_id, chunk) for doc_id, (score, chunk) in ranked_by_document.items()]
        ranked.sort(key=lambda value: (-value[0], value[2]["chunk_id"], value[1]))
        top = ranked[:5]
        expected = set(source_ids)
        hit_positions = [index for index, (_, doc_id, _) in enumerate(top, 1) if doc_id in expected]
        relevances = [1 if doc_id in expected else 0 for _, doc_id, _ in top]
        ideal = [1] * min(len(expected), 5)
        rows.append(
            {
                "case_id": case_id,
                "status": "MEASURED",
                "policy_status": "ELIGIBLE",
                "hit": bool(hit_positions),
                "reciprocal_rank": 1 / hit_positions[0] if hit_positions else 0.0,
                "ndcg_at_5": _dcg(relevances) / max(_dcg(ideal), 1.0),
                "result_document_ids": [doc_id for _, doc_id, _ in top],
            }
        )
    measured = [row for row in rows if row["status"] == "MEASURED"]
    report = {
        "report_version": "aligned-retrieval-v2",
        "eval_set_version": eval_set_version,
        "input_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "alignment_sha256": hashlib.sha256(alignment_path.read_bytes()).hexdigest() if alignment_path.exists() else None,
        "pipeline_version": "local-source-alignment-v1",
        "model_version": "deterministic-keyword-v1",
        "real_service_acceptance": False,
        "status": "PASS" if approved and not issues and all(
            row["status"] == "MEASURED" or row.get("refusal_match") is True
            for row in rows
        ) else "NOT_RUN",
        "metrics": _metrics(rows),
        "case_count": len(cases),
        "measured_case_count": len(measured),
        "status_counts": dict(collections.Counter(row["status"] for row in rows)),
        "policy_status_counts": dict(collections.Counter(
            row["policy_status"] for row in rows if row.get("policy_status")
        )),
        "refusal_counts": dict(refusal_counts),
        "issues": issues,
        "notes": [
            "Corpus content is parsed from the local synthetic source manifest.",
            "No case query or answer point is copied into corpus content.",
            "Metrics require an explicit reviewed alignment.json and remain separate from real-service acceptance.",
        ],
        "case_results": rows,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "case_count", "measured_case_count", "metrics", "real_service_acceptance")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
