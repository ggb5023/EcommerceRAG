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


def _load_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    cases = _load_lines(args.cases)
    corpus = _load_lines(args.corpus)
    alignment_path = args.alignment or args.corpus.with_name("alignment.json")
    alignment = json.loads(alignment_path.read_text(encoding="utf-8")) if alignment_path.exists() else {}
    corpus_by_id = {doc["document_id"]: doc for doc in corpus}
    mapping = alignment.get("case_to_source_documents", {})
    approved = alignment.get("status") == "APPROVED"
    rows: list[dict] = []
    issues: list[str] = []
    for case in cases:
        case_id = case["case_id"]
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
        missing = [doc_id for doc_id in source_ids if doc_id not in corpus_by_id]
        if missing:
            issues.append(f"{case_id}: missing source document(s): {','.join(missing)}")
            rows.append({"case_id": case_id, "status": "ALIGNMENT_INVALID", "missing": missing})
            continue
        query_tokens = tokens(case["query"])
        allowed = case["authorization"]
        ranked: list[tuple[int, str, dict]] = []
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
                    ranked.append((score, doc_id, chunk))
        ranked.sort(key=lambda value: (-value[0], value[2]["chunk_id"]))
        top = ranked[:5]
        expected = set(source_ids)
        hit_positions = [index for index, (_, doc_id, _) in enumerate(top, 1) if doc_id in expected]
        relevances = [1 if doc_id in expected else 0 for _, doc_id, _ in top]
        ideal = [1] * min(len(expected), 5)
        rows.append(
            {
                "case_id": case_id,
                "status": "MEASURED",
                "hit": bool(hit_positions),
                "reciprocal_rank": 1 / hit_positions[0] if hit_positions else 0.0,
                "ndcg_at_5": _dcg(relevances) / max(_dcg(ideal), 1.0),
                "result_document_ids": [doc_id for _, doc_id, _ in top],
            }
        )
    measured = [row for row in rows if row["status"] == "MEASURED"]
    report = {
        "report_version": "aligned-retrieval-v2",
        "eval_set_version": "synthetic-m2-v1",
        "input_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "alignment_sha256": hashlib.sha256(alignment_path.read_bytes()).hexdigest() if alignment_path.exists() else None,
        "pipeline_version": "local-source-alignment-v1",
        "model_version": "deterministic-keyword-v1",
        "real_service_acceptance": False,
        "status": "PASS" if approved and not issues and len(measured) == len(cases) else "NOT_RUN",
        "metrics": _metrics(rows),
        "case_count": len(cases),
        "measured_case_count": len(measured),
        "status_counts": dict(collections.Counter(row["status"] for row in rows)),
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
