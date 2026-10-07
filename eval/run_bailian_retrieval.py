#!/usr/bin/env python3
"""Run an isolated, metadata-only Alibaba Bailian retrieval evaluation.

The command is intentionally opt-in.  Without ``--live`` it performs only
input validation and never reads provider credentials or makes a network call.
The live path uses the approved synthetic aligned corpus, keeps all provider
responses in memory, and writes no vectors, prompts, answers, or customer data.
It is a model/provider experiment, not M1 or real-service acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from answer_point_evidence import validate_review
from app.providers import build_provider
from app.providers.config import (
    ProviderConfigError,
    ensure_secure_config_file,
    load_provider_config,
)
from app.providers.contracts import ProviderError

DEFAULT_CASES = ROOT / "eval" / "synthetic_cases.jsonl"
DEFAULT_CORPUS = Path(
    "/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/documents.jsonl"
)
DEFAULT_ALIGNMENT = DEFAULT_CORPUS.with_name("alignment-revision-1.json")
DEFAULT_ANSWER_EVIDENCE = DEFAULT_CORPUS.with_name("answer-point-evidence-review-v1.json")
DEFAULT_METADATA = ROOT / "eval" / "synthetic_cases.metadata.json"
DEFAULT_ENV = Path("/etc/ecommerce-rag/providers.env")
GENERATION_INSTRUCTION = (
    "Return concise answer_points supported directly by the evidence. Preserve "
    "important product names, numbers, dates, units, and qualifiers exactly as "
    "they appear in the evidence. Do not invent facts or weaken a restriction; "
    "omit a point when the evidence does not support it."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def tokens(value: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9_]+", value.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        for size in (2, 3, 4):
            result.update(run[index : index + size] for index in range(len(run) - size + 1))
    return result


def _token_overlap_supported(expected: str, generated: str, *, threshold: float = 0.5) -> bool:
    """Report a diagnostic overlap without treating it as answer acceptance."""
    expected_tokens = tokens(expected)
    generated_tokens = tokens(generated)
    if not expected_tokens or not generated_tokens:
        return False
    return len(expected_tokens & generated_tokens) / len(expected_tokens) >= threshold


def _answer_point_diagnostics(
    expected_points: Sequence[str],
    generated_points: Sequence[str],
    evidence: str,
    approved_evidence_supported: Sequence[bool] | None = None,
) -> dict[str, Any]:
    """Summarize answer-point support without retaining source or model text."""
    expected = [point for point in expected_points if isinstance(point, str) and point.strip()]
    generated = [point for point in generated_points if isinstance(point, str) and point.strip()]
    evidence_exact = [point in evidence for point in expected]
    evidence_token = [
        _token_overlap_supported(point, evidence)
        for point in expected
    ]
    generated_exact = [
        any(point in candidate for candidate in generated)
        for point in expected
    ]
    generated_token = [
        any(_token_overlap_supported(point, candidate) for candidate in generated)
        for point in expected
    ]
    if approved_evidence_supported is not None:
        if len(approved_evidence_supported) != len(expected):
            raise ValueError("approved evidence support count does not match expected points")
        reviewed_support = [bool(value) for value in approved_evidence_supported]
    else:
        reviewed_support = [False] * len(expected)
    evidence_supported = [
        lexical or reviewed
        for lexical, reviewed in zip(evidence_token, reviewed_support)
    ]
    classifications: list[str] = []
    for evidence_is_supported, exact, token_match in zip(
        evidence_supported,
        generated_exact,
        generated_token,
    ):
        if not evidence_is_supported:
            classification = "evidence_unsupported"
        elif not generated:
            classification = "generation_empty"
        elif exact or token_match:
            classification = "generated_supported"
        else:
            classification = "generation_rewrite_mismatch"
        classifications.append(classification)
    classification_counts = {
        name: classifications.count(name)
        for name in (
            "generated_supported",
            "evidence_unsupported",
            "generation_empty",
            "generation_rewrite_mismatch",
        )
        if name in classifications
    }
    return {
        "expected_answer_point_fingerprints": [fingerprint(point) for point in expected],
        "generated_answer_point_fingerprints": [fingerprint(point) for point in generated],
        "generated_answer_point_count": len(generated),
        "evidence_exact_answer_point_match_count": sum(evidence_exact),
        "evidence_token_overlap_answer_point_match_count": sum(evidence_token),
        "approved_evidence_answer_point_match_count": sum(reviewed_support),
        "evidence_supported_answer_point_count": sum(evidence_supported),
        "generated_exact_answer_point_match_count": sum(generated_exact),
        "generated_token_overlap_answer_point_match_count": sum(generated_token),
        "answer_point_diagnostic_counts": classification_counts,
        "answer_point_diagnostics": [
            {
                "expected_fingerprint": fingerprint(point),
                "evidence_exact_supported": exact,
                "evidence_token_overlap_supported": token,
                "approved_evidence_supported": reviewed_support[index],
                "evidence_supported": evidence_supported[index],
                "generated_exact_supported": generated_exact[index],
                "generated_token_overlap_supported": generated_token[index],
                "classification": classifications[index],
            }
            for index, (point, exact, token) in enumerate(zip(expected, evidence_exact, evidence_token))
        ],
    }


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return product / (left_norm * right_norm)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("JSONL rows must be objects")
            rows.append(value)
    return rows


def load_inputs(
    cases_path: Path,
    corpus_path: Path,
    alignment_path: Path,
    metadata_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        cases = load_jsonl(cases_path)
        corpus = load_jsonl(corpus_path)
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return [], [], {}, [f"input_invalid:{type(error).__name__}"]
    if len(cases) != 60:
        issues.append(f"case_count:{len(cases)}")
    if metadata.get("sha256") != sha256(cases_path):
        issues.append("case_sha256_mismatch")
    if alignment.get("status") != "APPROVED":
        issues.append("alignment_not_approved")
    if alignment.get("real_service_acceptance") is not False:
        issues.append("alignment_real_service_acceptance_not_false")
    mapping = alignment.get("case_to_source_documents")
    if not isinstance(mapping, dict) or len(mapping) != len(cases):
        issues.append("alignment_case_set_mismatch")
    corpus_ids = [row.get("document_id") for row in corpus]
    if any(not isinstance(value, str) or not value for value in corpus_ids):
        issues.append("corpus_document_id_invalid")
    if len(corpus_ids) != len(set(corpus_ids)):
        issues.append("duplicate_corpus_document_id")
    issues.extend(validate_corpus_versions(corpus))
    return cases, corpus, alignment, issues


def validate_corpus_versions(corpus: Sequence[Mapping[str, Any]]) -> list[str]:
    """Require every chunk to remain bound to its document version and scope."""
    issues: list[str] = []
    seen_chunks: set[str] = set()
    for document in corpus:
        document_id = document.get("document_id")
        version_id = document.get("document_version_id")
        tenant_id = document.get("tenant_id")
        shop_id = document.get("shop_id")
        if not isinstance(version_id, str) or not version_id:
            issues.append(f"corpus_document_version_id_invalid:{document_id or '<missing>'}")
            continue
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(shop_id, str) or not shop_id:
            issues.append(f"corpus_document_scope_invalid:{document_id or '<missing>'}")
        chunks = document.get("chunks", [])
        if not isinstance(chunks, list):
            issues.append(f"corpus_chunks_invalid:{document_id or '<missing>'}")
            continue
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                issues.append(f"corpus_chunk_invalid:{document_id or '<missing>'}")
                continue
            chunk_id = chunk.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                issues.append(f"corpus_chunk_id_invalid:{document_id or '<missing>'}")
            elif chunk_id in seen_chunks:
                issues.append(f"duplicate_corpus_chunk_id:{chunk_id}")
            else:
                seen_chunks.add(chunk_id)
            if chunk.get("document_id") != document_id:
                issues.append(f"corpus_chunk_document_mismatch:{chunk_id or '<missing>'}")
            if chunk.get("document_version_id") != version_id:
                issues.append(f"corpus_chunk_version_mismatch:{chunk_id or '<missing>'}")
            if chunk.get("tenant_id") != tenant_id or chunk.get("shop_id") != shop_id:
                issues.append(f"corpus_chunk_scope_mismatch:{chunk_id or '<missing>'}")
    return sorted(set(issues))


def select_cases(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or limit > len(cases):
        raise ValueError("limit must be between 1 and the case count")
    return sorted(cases, key=lambda row: str(row.get("case_id", "")))[:limit]


def _meta(slot: str, results: object) -> dict[str, Any]:
    values = list(results) if slot == "embedding" and isinstance(results, (list, tuple)) else [results]
    request_ids = [getattr(value, "request_id", "") for value in values]
    models = [getattr(value, "model", "") for value in values]
    usages = [getattr(value, "usage", None) for value in values]
    complete = bool(values) and all(
        isinstance(value, str) and value.strip() for value in request_ids
    ) and all(value is not None for value in usages)
    return {
        "slot": slot,
        "model_fingerprint": fingerprint(models[0]) if models else None,
        "request_id_fingerprint": fingerprint(request_ids[0]) if request_ids else None,
        "request_id_present": all(bool(value) for value in request_ids),
        "usage_present": all(value is not None for value in usages),
        "metadata_complete": complete,
        "result_count": len(values),
    }


def _expected_actual_ids(case: dict[str, Any], alignment: Mapping[str, Any]) -> set[str]:
    case_mapping = alignment.get("case_to_source_documents", {}).get(case.get("case_id"), {})
    return set(case_mapping.values()) if isinstance(case_mapping, Mapping) else set()


def _eligible_chunks(case: dict[str, Any], corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    authorization = case.get("authorization", {})
    tenant_id = authorization.get("tenant_id")
    shop_id = authorization.get("shop_id")
    chunks: list[dict[str, Any]] = []
    for document in corpus:
        if document.get("tenant_id") != tenant_id or document.get("shop_id") != shop_id:
            continue
        for chunk in document.get("chunks", []):
            if not isinstance(chunk, dict) or chunk.get("disclosure_class") != "external_allowed":
                continue
            chunks.append(chunk)
    return chunks


def _rank_by_embedding(
    query_embedding: Sequence[float],
    chunk_embeddings: Sequence[Sequence[float]],
    chunks: Sequence[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    ranked = sorted(
        zip(chunks, chunk_embeddings),
        key=lambda item: (-cosine(query_embedding, item[1]), str(item[0].get("chunk_id", ""))),
    )
    return [
        {
            "chunk_id": chunk.get("chunk_id"),
            "document_id": chunk.get("document_id"),
            "document_version_id": chunk.get("document_version_id"),
            "score": round(cosine(query_embedding, vector), 8),
        }
        for chunk, vector in ranked[:limit]
    ]


def _rank_by_rerank(result: Any, candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in result.items:
        if 0 <= item.index < len(candidates):
            candidate = candidates[item.index]
            rows.append({
                "chunk_id": candidate.get("chunk_id"),
                "document_id": candidate.get("document_id"),
                "document_version_id": candidate.get("document_version_id"),
                "score": round(float(item.score), 8),
            })
    return rows


def _hit(rows: Sequence[Mapping[str, Any]], expected_ids: set[str]) -> bool:
    return any(row.get("document_id") in expected_ids for row in rows[:5])


def not_run_report(
    cases_path: Path,
    corpus_path: Path,
    alignment_path: Path,
    issues: list[str],
) -> dict[str, Any]:
    return {
        "report_version": "bailian-retrieval-v2",
        "provider_profile": "aliyun-bailian",
        "input_sha256": sha256(cases_path) if cases_path.is_file() else None,
        "corpus_sha256": sha256(corpus_path) if corpus_path.is_file() else None,
        "alignment_sha256": sha256(alignment_path) if alignment_path.is_file() else None,
        "model_quality_claim": False,
        "real_service_acceptance": False,
        "m1_connected": False,
        "online_requests_made": False,
        "status": "NOT_RUN",
        "issues": sorted(set(issues)),
        "case_results": [],
    }


async def run_live(
    provider: Any,
    cases: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    alignment: Mapping[str, Any],
    *,
    batch_size: int = 16,
    answer_evidence: Mapping[str, Sequence[bool]] | None = None,
) -> dict[str, Any]:
    selected_chunks: list[dict[str, Any]] = []
    for case in cases:
        for chunk in _eligible_chunks(case, corpus):
            if chunk not in selected_chunks:
                selected_chunks.append(chunk)
    documents = [str(chunk.get("content", "")) for chunk in selected_chunks]
    slot_reports: list[dict[str, Any]] = []
    document_embeddings: list[Any] = []
    started = time.monotonic()
    try:
        for start in range(0, len(documents), batch_size):
            result = await provider.embed(
                documents[start : start + batch_size],
                text_type="document",
                dimensions=1024,
                output_type="dense&sparse",
            )
            document_embeddings.extend(result)
        slot_reports.append({**_meta("embedding", document_embeddings), "latency_ms": round((time.monotonic() - started) * 1000, 1)})
    except ProviderError as error:
        return {
            "status": "FAIL",
            "issues": [f"embedding:{error.code}"],
            "slot_reports": slot_reports,
        }
    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        expected_ids = _expected_actual_ids(case, alignment)
        query = str(case.get("query", ""))
        case_started = time.monotonic()
        try:
            query_result = await provider.embed(
                [query],
                text_type="query",
                dimensions=1024,
                output_type="dense&sparse",
            )
            slot_reports.append({
                **_meta("embedding", query_result),
                "operation": "query",
                "case_id": case_id,
            })
            embedding_rows = _rank_by_embedding(query_result[0].dense, [item.dense for item in document_embeddings], selected_chunks, 10)
            rerank_candidates = [chunk for chunk in selected_chunks if chunk.get("chunk_id") in {row["chunk_id"] for row in embedding_rows}]
            embedding_order = {row["chunk_id"]: index for index, row in enumerate(embedding_rows)}
            rerank_candidates.sort(key=lambda item: embedding_order.get(item.get("chunk_id"), len(embedding_rows)))
            if not rerank_candidates:
                results.append({"case_id": case_id, "status": "FAIL", "error_code": "no_candidates"})
                continue
            rerank_result = await provider.rerank(query, [str(chunk.get("content", "")) for chunk in rerank_candidates], top_n=min(5, len(rerank_candidates)))
            slot_reports.append({
                **_meta("rerank", rerank_result),
                "operation": "rerank",
                "case_id": case_id,
            })
            rerank_rows = _rank_by_rerank(rerank_result, rerank_candidates)
            rerank_order = {
                row["chunk_id"]: index
                for index, row in enumerate(rerank_rows)
                if row.get("chunk_id")
            }
            evidence_chunks = sorted(
                rerank_candidates,
                key=lambda chunk: rerank_order.get(chunk.get("chunk_id"), len(rerank_rows)),
            )
            evidence = "\n\n".join(
                str(chunk.get("content", "")) for chunk in evidence_chunks[:5]
            )
            schema = {"name": "answer", "schema": {"type": "object", "required": ["answer_points"], "properties": {"answer_points": {"type": "array", "items": {"type": "string"}}}}}
            generation = await provider.generate([{
                "role": "user",
                "content": (
                    f"{GENERATION_INSTRUCTION}\n\n"
                    f"Query: {query}\nEvidence:\n{evidence}"
                ),
            }], response_schema=schema)
            slot_reports.append({
                **_meta("generation", generation),
                "operation": "generation",
                "case_id": case_id,
            })
            generated_points = (
                generation.structured.get("answer_points", [])
                if generation.structured
                else []
            )
            if not isinstance(generated_points, list):
                generated_points = []
            generated_points = [point for point in generated_points if isinstance(point, str)]
            expected_points = [str(point) for point in case.get("expected_answer_points", [])]
            answer_diagnostics = _answer_point_diagnostics(
                expected_points,
                generated_points,
                evidence,
                (answer_evidence or {}).get(case_id),
            )
            results.append({
                "case_id": case_id,
                "expected_document_ids": sorted(expected_ids),
                "embedding_top5": embedding_rows[:5],
                "rerank_top5": rerank_rows[:5],
                "embedding_hit_at_5": _hit(embedding_rows, expected_ids),
                "rerank_hit_at_5": _hit(rerank_rows, expected_ids),
                "generation_structured": generation.structured is not None,
                "expected_answer_point_count": len(expected_points),
                **answer_diagnostics,
                "matched_answer_point_count": answer_diagnostics["generated_exact_answer_point_match_count"],
                "normalized_answer_point_match_count": answer_diagnostics["generated_token_overlap_answer_point_match_count"],
                "answer_point_match_method": "exact_substring_v1",
                "normalized_answer_point_diagnostic": "token_overlap_v1",
                "latency_ms": round((time.monotonic() - case_started) * 1000, 1),
            })
        except ProviderError as error:
            results.append({"case_id": case_id, "status": "FAIL", "error_code": error.code})
    return {
        "status": "PASS" if all(row.get("status") != "FAIL" for row in results) else "FAIL",
        "slot_reports": slot_reports,
        "case_results": results,
        "issues": [],
    }


async def main_async(args: argparse.Namespace) -> int:
    cases, corpus, alignment, issues = load_inputs(args.cases, args.corpus, args.alignment, args.metadata)
    answer_evidence: dict[str, list[bool]] = {}
    answer_review: dict[str, Any] = {
        "status": "NOT_RUN",
        "review_sha256": None,
        "issues": [],
    }
    if args.answer_evidence and args.answer_evidence.is_file() and not issues:
        answer_evidence, answer_review = validate_review(
            args.answer_evidence,
            args.cases,
            args.corpus,
            args.alignment,
            cases,
            corpus,
            alignment,
        )
        issues.extend(f"answer_evidence:{issue}" for issue in answer_review.get("issues", []))
    if issues or not args.live:
        report = not_run_report(args.cases, args.corpus, args.alignment, issues + ([] if args.live else ["live_flag_required"]))
        print(json.dumps({"status": report["status"], "issues": report["issues"], "real_service_acceptance": False}, ensure_ascii=False))
        return 0
    try:
        selected = select_cases(cases, args.limit)
        ensure_secure_config_file(args.env)
        config = load_provider_config(args.env)
        if config.profile != "aliyun-bailian":
            raise ProviderConfigError("provider profile is not aliyun-bailian")
        provider = build_provider(config)
        run = await run_live(
            provider,
            selected,
            corpus,
            alignment,
            answer_evidence=answer_evidence,
        )
    except (ProviderConfigError, ValueError) as error:
        report = not_run_report(args.cases, args.corpus, args.alignment, [f"configuration:{type(error).__name__}"])
        print(json.dumps({"status": report["status"], "issues": report["issues"], "real_service_acceptance": False}, ensure_ascii=False))
        return 3
    report = {
        "report_version": "bailian-retrieval-v2",
        "provider_profile": "aliyun-bailian",
        "case_count": len(selected),
        "input_sha256": sha256(args.cases),
        "corpus_sha256": sha256(args.corpus),
        "alignment_sha256": sha256(args.alignment),
        "model_quality_claim": False,
        "real_service_acceptance": False,
        "m1_connected": False,
        "online_requests_made": True,
        "answer_evidence_review_status": answer_review.get("status", "NOT_RUN"),
        "answer_evidence_review_sha256": answer_review.get("review_sha256"),
        "answer_evidence_review_counts": {
            key: answer_review.get(key, 0)
            for key in ("point_count", "approved_point_count", "unresolved_point_count")
        },
        **run,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "case_count", "real_service_acceptance", "m1_connected")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="make isolated Bailian requests")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--alignment", type=Path, default=DEFAULT_ALIGNMENT)
    parser.add_argument("--answer-evidence", type=Path, default=DEFAULT_ANSWER_EVIDENCE)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
