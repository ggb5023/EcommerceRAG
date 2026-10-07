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
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import date
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
GENERATION_CONTEXT_CHUNK_LIMIT = 5


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


QUALIFIER_MARKERS = {
    "polarity": ("不得", "不应", "不能", "不可", "禁止", "不允许", "未", "没有", "不是", "no", "not", "never", "cannot", "must not", "without"),
    "scope": ("仅限", "仅", "只能", "至少", "最多", "不超过", "以内", "除外", "除非", "only", "at least", "at most", "no more than", "up to", "within", "except", "unless", "maximum", "minimum"),
    "modality": ("必须", "应当", "需要", "建议", "可以", "must", "should", "required", "recommended", "may", "can"),
    "uncertainty": ("可能", "通常", "一般", "不一定", "不保证", "无法保证", "possibly", "usually", "generally", "not guaranteed", "cannot guarantee"),
}
UNIT_MARKERS = {
    "temperature": ("℃", "°c", "摄氏度"),
    "length": ("centimeters", "centimeter", "millimeters", "millimeter", "inches", "inch", "厘米", "毫米", "英寸", "cm", "mm"),
    "mass": ("kilograms", "kilogram", "grams", "gram", "公斤", "千克", "克", "kg", "g"),
    "volume": ("milliliters", "milliliter", "liters", "liter", "毫升", "升", "ml", "l"),
    "time": ("business days", "calendar days", "minutes", "minute", "hours", "hour", "weeks", "week", "months", "month", "years", "year", "days", "day", "工作日", "天", "小时", "分钟", "周", "个月", "年"),
}


def _contains_marker(text: str, marker: str) -> bool:
    if marker.isascii() and marker.replace(" ", "").isalpha():
        return re.search(rf"\b{re.escape(marker)}\b", text, re.IGNORECASE) is not None
    return marker in text


def _script_profile(text: str) -> str:
    han_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    if han_count and latin_count:
        return "mixed"
    if han_count:
        return "zh"
    if latin_count:
        return "en"
    return "other"


def _answer_point_composition_diagnostics(
    expected_points: Sequence[str], generated_points: Sequence[str]
) -> dict[str, Any]:
    """Emit heuristic composition signals without retaining answer text."""
    generated = [point for point in generated_points if isinstance(point, str) and point.strip()]
    generated_text = "\n".join(generated)
    generated_token_set = set().union(*(tokens(point) for point in generated)) if generated else set()
    split_candidates = 0
    aggregate_matches = 0
    multi_expected_generated_points = 0
    language_counts = {
        relation: 0 for relation in ("same_script", "opposite_script", "mixed", "unclassified", "generated_missing")
    }
    details: list[dict[str, Any]] = []
    for point in expected_points:
        expected = point if isinstance(point, str) else ""
        expected_tokens = tokens(expected)
        individual_matches = sum(_token_overlap_supported(expected, item) for item in generated)
        aggregate_match = bool(expected_tokens) and (
            len(expected_tokens & generated_token_set) / len(expected_tokens) >= 0.5
        )
        split_candidate = aggregate_match and individual_matches == 0 and len(generated) > 1
        aggregate_matches += aggregate_match
        split_candidates += split_candidate

        expected_profile = _script_profile(expected)
        generated_profile = _script_profile(generated_text)
        if not generated:
            language_relation = "generated_missing"
        elif expected_profile in {"zh", "en"} and generated_profile in {"zh", "en"}:
            language_relation = "same_script" if expected_profile == generated_profile else "opposite_script"
        elif expected_profile == generated_profile == "mixed":
            language_relation = "mixed"
        else:
            language_relation = "unclassified"
        language_counts[language_relation] += 1

        expected_qualifiers = {
            marker
            for markers in QUALIFIER_MARKERS.values()
            for marker in markers
            if _contains_marker(expected, marker)
        }
        generated_qualifiers = {
            marker
            for markers in QUALIFIER_MARKERS.values()
            for marker in markers
            if _contains_marker(generated_text, marker)
        }
        expected_categories = {
            category
            for category, markers in QUALIFIER_MARKERS.items()
            if any(_contains_marker(expected, marker) for marker in markers)
        }
        generated_categories = {
            category
            for category, markers in QUALIFIER_MARKERS.items()
            if any(_contains_marker(generated_text, marker) for marker in markers)
        }
        expected_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", expected))
        generated_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", generated_text))
        expected_units = {
            category
            for category, markers in UNIT_MARKERS.items()
            if any(_contains_marker(expected, marker) for marker in markers)
        }
        generated_units = {
            category
            for category, markers in UNIT_MARKERS.items()
            if any(_contains_marker(generated_text, marker) for marker in markers)
        }
        details.append({
            "expected_fingerprint": fingerprint(expected),
            "individual_generated_point_overlap_count": individual_matches,
            "aggregate_generated_point_overlap": aggregate_match,
            "split_across_generated_points_candidate": split_candidate,
            "language_relation": language_relation,
            "expected_qualifier_marker_count": len(expected_qualifiers),
            "missing_qualifier_marker_count": len(expected_qualifiers - generated_qualifiers),
            "expected_qualifier_category_count": len(expected_categories),
            "missing_qualifier_category_count": len(expected_categories - generated_categories),
            "expected_numeric_literal_count": len(expected_numbers),
            "matched_numeric_literal_count": len(expected_numbers & generated_numbers),
            "expected_unit_category_count": len(expected_units),
            "matched_unit_category_count": len(expected_units & generated_units),
        })

    for generated_point in generated:
        expected_overlap_count = sum(
            _token_overlap_supported(point, generated_point) for point in expected_points
        )
        multi_expected_generated_points += expected_overlap_count >= 2

    return {
        "answer_point_composition_diagnostic_version": "answer-point-composition-v1",
        "answer_point_composition_is_heuristic": True,
        "aggregate_generated_token_overlap_answer_point_count": aggregate_matches,
        "expected_points_with_split_overlap_candidate_count": split_candidates,
        "generated_points_with_multi_expected_overlap_candidate_count": multi_expected_generated_points,
        "language_relation_counts": language_counts,
        "answer_point_composition_diagnostics": details,
    }


def _answer_point_diagnostics(
    expected_points: Sequence[str],
    generated_points: Sequence[str],
    evidence: str,
    approved_evidence_supported: Sequence[bool] | None = None,
    delivered_approved_evidence_supported: Sequence[bool] | None = None,
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
    if delivered_approved_evidence_supported is not None:
        if len(delivered_approved_evidence_supported) != len(expected):
            raise ValueError("delivered evidence support count does not match expected points")
        delivered_support = [
            reviewed and bool(delivered)
            for reviewed, delivered in zip(reviewed_support, delivered_approved_evidence_supported)
        ]
    else:
        delivered_support = [False] * len(expected)
    evidence_supported = [
        delivered if approved_evidence_supported is not None else lexical
        for lexical, delivered in zip(evidence_token, delivered_support)
    ]
    classifications: list[str] = []
    for reviewed, delivered, evidence_is_supported, exact, token_match in zip(
        reviewed_support,
        delivered_support,
        evidence_supported,
        generated_exact,
        generated_token,
    ):
        if reviewed and not delivered:
            classification = "approved_evidence_not_delivered"
        elif not evidence_is_supported:
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
            "approved_evidence_not_delivered",
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
        "delivered_approved_evidence_answer_point_match_count": sum(delivered_support),
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
                "delivered_approved_evidence_supported": delivered_support[index],
                "evidence_supported": evidence_supported[index],
                "generated_exact_supported": generated_exact[index],
                "generated_token_overlap_supported": generated_token[index],
                "classification": classifications[index],
            }
            for index, (point, exact, token) in enumerate(zip(expected, evidence_exact, evidence_token))
        ],
        **_answer_point_composition_diagnostics(expected, generated),
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
    for case in cases:
        try:
            _calendar_date(case.get("business_date"))
        except ValueError:
            issues.append(f"business_date_invalid:{case.get('case_id', '<missing>')}")
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
        try:
            _in_business_date(document, date.min)
        except ValueError:
            issues.append(f"corpus_document_dates_invalid:{document_id or '<missing>'}")
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
            try:
                _in_business_date(chunk, date.min)
            except ValueError:
                issues.append(f"corpus_chunk_dates_invalid:{chunk_id or '<missing>'}")
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
    if not isinstance(authorization, Mapping):
        return []
    tenant_id = authorization.get("tenant_id")
    shop_id = authorization.get("shop_id")
    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(shop_id, str) or not shop_id:
        return []
    current = _calendar_date(case.get("business_date"))
    chunks: list[dict[str, Any]] = []
    for document in corpus:
        if document.get("tenant_id") != tenant_id or document.get("shop_id") != shop_id:
            continue
        if not _in_business_date(document, current):
            continue
        for chunk in document.get("chunks", []):
            if not isinstance(chunk, dict) or chunk.get("disclosure_class") != "external_allowed":
                continue
            if chunk.get("tenant_id") != tenant_id or chunk.get("shop_id") != shop_id:
                continue
            if not _in_business_date(chunk, current):
                continue
            chunks.append(chunk)
    return chunks


def _calendar_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid calendar date")
    return date.fromisoformat(value)


def _in_business_date(row: Mapping[str, Any], current: date) -> bool:
    start = _calendar_date(row["effective_from"]) if row.get("effective_from") is not None else None
    end = _calendar_date(row["effective_to"]) if row.get("effective_to") is not None else None
    if start and end and end <= start:
        raise ValueError("invalid effective date range")
    return not ((start and current < start) or (end and current >= end))


def audit_generation_context(
    cases: Sequence[dict[str, Any]],
    corpus: list[dict[str, Any]],
    approved_bindings: Mapping[str, Sequence[Sequence[str]]],
) -> dict[str, Any]:
    """Check support feasibility without selecting evidence or contacting a model."""
    rows: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        eligible_ids = {chunk["chunk_id"] for chunk in _eligible_chunks(case, corpus)}
        points = case.get("expected_answer_points", [])
        bindings = approved_bindings.get(case_id, [])
        required_by_point = [set(bindings[index]) if index < len(bindings) else set() for index in range(len(points))]
        required = set().union(*required_by_point) if required_by_point else set()
        missing = required - eligible_ids
        if not required_by_point or any(not chunk_ids for chunk_ids in required_by_point):
            status = "UNREVIEWED"
        elif missing:
            status = "INELIGIBLE_REQUIRED_CHUNKS"
        elif len(required) > GENERATION_CONTEXT_CHUNK_LIMIT:
            status = "BUDGET_EXCEEDED"
        else:
            status = "WITHIN_BUDGET"
        rows.append({
            "case_id": case_id,
            "status": status,
            "required_chunk_count": len(required),
            "eligible_required_chunk_count": len(required & eligible_ids),
            "ineligible_required_chunk_ids": sorted(missing),
            "point_required_chunk_counts": [len(chunk_ids) for chunk_ids in required_by_point],
        })
    return {
        "report_version": "generation-context-audit-v1",
        "case_count": len(rows),
        "context_chunk_limit": GENERATION_CONTEXT_CHUNK_LIMIT,
        "status_counts": {
            status: sum(row["status"] == status for row in rows)
            for status in ("WITHIN_BUDGET", "BUDGET_EXCEEDED", "INELIGIBLE_REQUIRED_CHUNKS", "UNREVIEWED")
        },
        "generation_context_selection": "NOT_RUN",
        "online_requests_made": False,
        "answer_quality_status": "NOT_RUN",
        "model_quality_claim": False,
        "real_service_acceptance": False,
        "case_results": rows,
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    """Publish a restricted report atomically, preserving any existing evidence."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
        "report_version": "bailian-retrieval-v3",
        "provider_profile": "aliyun-bailian",
        "input_sha256": sha256(cases_path) if cases_path.is_file() else None,
        "corpus_sha256": sha256(corpus_path) if corpus_path.is_file() else None,
        "alignment_sha256": sha256(alignment_path) if alignment_path.is_file() else None,
        "model_quality_claim": False,
        "answer_quality_status": "NOT_RUN",
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
    answer_evidence_chunks: Mapping[str, Sequence[Sequence[str]]] | None = None,
) -> dict[str, Any]:
    selected_chunks: list[dict[str, Any]] = []
    eligible_by_case = {str(case["case_id"]): _eligible_chunks(case, corpus) for case in cases}
    for chunks in eligible_by_case.values():
        for chunk in chunks:
            if chunk not in selected_chunks:
                selected_chunks.append(chunk)
    if not selected_chunks:
        return {"status": "FAIL", "issues": ["no_eligible_chunks"], "case_results": [], "slot_reports": []}
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
    embedding_by_chunk = {
        chunk["chunk_id"]: embedding for chunk, embedding in zip(selected_chunks, document_embeddings)
    }
    for case in cases:
        case_id = str(case["case_id"])
        expected_ids = _expected_actual_ids(case, alignment)
        query = str(case.get("query", ""))
        case_started = time.monotonic()
        try:
            eligible_chunks = eligible_by_case[case_id]
            if not eligible_chunks:
                results.append({"case_id": case_id, "status": "FAIL", "error_code": "no_eligible_chunks"})
                continue
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
            embedding_rows = _rank_by_embedding(query_result[0].dense, [embedding_by_chunk[chunk["chunk_id"]].dense for chunk in eligible_chunks], eligible_chunks, 10)
            rerank_candidates = [chunk for chunk in eligible_chunks if chunk.get("chunk_id") in {row["chunk_id"] for row in embedding_rows}]
            embedding_order = {row["chunk_id"]: index for index, row in enumerate(embedding_rows)}
            rerank_candidates.sort(key=lambda item: embedding_order.get(item.get("chunk_id"), len(embedding_rows)))
            if not rerank_candidates:
                results.append({"case_id": case_id, "status": "FAIL", "error_code": "no_candidates"})
                continue
            rerank_result = await provider.rerank(query, [str(chunk.get("content", "")) for chunk in rerank_candidates], top_n=min(GENERATION_CONTEXT_CHUNK_LIMIT, len(rerank_candidates)))
            slot_reports.append({
                **_meta("rerank", rerank_result),
                "operation": "rerank",
                "case_id": case_id,
            })
            rerank_rows = _rank_by_rerank(rerank_result, rerank_candidates)
            candidate_by_id = {chunk["chunk_id"]: chunk for chunk in rerank_candidates}
            evidence_chunks = [candidate_by_id[row["chunk_id"]] for row in rerank_rows[:GENERATION_CONTEXT_CHUNK_LIMIT]]
            if not evidence_chunks:
                results.append({"case_id": case_id, "status": "FAIL", "error_code": "no_rerank_evidence"})
                continue
            evidence = "\n\n".join(
                str(chunk.get("content", "")) for chunk in evidence_chunks
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
            delivered_ids = {chunk["chunk_id"] for chunk in evidence_chunks}
            required_chunks = (answer_evidence_chunks or {}).get(case_id, [])
            if required_chunks and len(required_chunks) != len(expected_points):
                raise ValueError("approved evidence chunk count does not match expected points")
            delivered_support = [
                bool(chunk_ids) and set(chunk_ids).issubset(delivered_ids)
                for chunk_ids in required_chunks
            ] if required_chunks else [False] * len(expected_points)
            answer_diagnostics = _answer_point_diagnostics(
                expected_points,
                generated_points,
                evidence,
                (answer_evidence or {}).get(case_id),
                delivered_support,
            )
            results.append({
                "case_id": case_id,
                "expected_document_ids": sorted(expected_ids),
                "embedding_top5": embedding_rows[:5],
                "rerank_top5": rerank_rows[:5],
                "embedding_hit_at_5": _hit(embedding_rows, expected_ids),
                "rerank_hit_at_5": _hit(rerank_rows, expected_ids),
                "generation_structured": generation.structured is not None,
                "generation_evidence": [
                    {
                        key: chunk.get(key)
                        for key in ("chunk_id", "document_id", "document_version_id", "chunk_hash")
                    }
                    for chunk in evidence_chunks
                ],
                "approved_support_coverage_method": "all_bound_chunks_v1",
                "approved_support_chunk_coverage": [
                    {
                        "point_index": index,
                        "required_chunk_count": len(chunk_ids),
                        "delivered_chunk_count": len(set(chunk_ids) & delivered_ids),
                        "all_delivered": delivered_support[index],
                    }
                    for index, chunk_ids in enumerate(required_chunks)
                ],
                "answer_quality_status": "NOT_RUN",
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
    if args.output and os.path.lexists(args.output):
        print(json.dumps({"status": "FAIL", "issues": ["output_exists"], "online_requests_made": False}))
        return 2
    if getattr(args, "audit_context", False):
        if issues:
            print(json.dumps({"status": "FAIL", "issues": issues, "online_requests_made": False}))
            return 2
        try:
            report = audit_generation_context(
                select_cases(cases, args.limit), corpus,
                answer_review.get("approved_support_chunk_ids", {}),
            )
        except ValueError:
            print(json.dumps({"status": "FAIL", "issues": ["context_audit_input_invalid"]}))
            return 2
        report.update({
            "input_sha256": sha256(args.cases),
            "corpus_sha256": sha256(args.corpus),
            "alignment_sha256": sha256(args.alignment),
            "answer_evidence_review_sha256": answer_review.get("review_sha256"),
        })
        if args.output:
            write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
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
            answer_evidence_chunks=answer_review.get("approved_support_chunk_ids", {}),
        )
    except (ProviderConfigError, ValueError) as error:
        report = not_run_report(args.cases, args.corpus, args.alignment, [f"configuration:{type(error).__name__}"])
        print(json.dumps({"status": report["status"], "issues": report["issues"], "real_service_acceptance": False}, ensure_ascii=False))
        return 3
    report = {
        "report_version": "bailian-retrieval-v3",
        "provider_profile": "aliyun-bailian",
        "case_count": len(selected),
        "input_sha256": sha256(args.cases),
        "corpus_sha256": sha256(args.corpus),
        "alignment_sha256": sha256(args.alignment),
        "model_quality_claim": False,
        "answer_quality_status": "NOT_RUN",
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
        write_report(args.output, report)
    print(json.dumps({key: report[key] for key in ("status", "case_count", "real_service_acceptance", "m1_connected")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="make isolated Bailian requests")
    mode.add_argument("--audit-context", action="store_true", help="audit approved evidence against scope/date and context size without a Provider call")
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
