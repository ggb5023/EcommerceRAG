#!/usr/bin/env python3
"""Run a local-only product retrieval baseline for an approved public source.

This track is intentionally independent from ``synthetic_cases.jsonl``.  It
does not call a model or the network.  A source that is not both downloaded
and terms/license verified produces ``NOT_RUN`` without reading an input file.
Raw input must live outside the repository and is never copied by this tool.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from validate_public_sources import file_sha256, validate_manifest

PIPELINE_VERSION = "public-product-baseline-v1"
DEFAULT_MAPPING = {
    "query_id": ["query_id", "qid", "query"],
    "product_id": ["product_id", "doc_id", "item_id", "asin"],
    "relevance": ["relevance", "relevance_label", "label", "is_relevant"],
    "rank": ["rank", "position"],
}


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_manifest(payload)
    if errors:
        raise ValueError("invalid public source manifest: " + "; ".join(errors))
    return payload


def load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise TypeError(f"record at line {line_number} must be an object")
        records.append(row)
    return records


def choose(row: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def parse_number(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"relevance/rank must be numeric: {value!r}") from exc


def normalize_records(records: list[dict[str, Any]], mapping: dict[str, list[str]]) -> tuple[list[dict[str, Any]], int]:
    normalized: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    duplicates = 0
    for row in records:
        product_id = choose(row, mapping["product_id"])
        if product_id is None or not str(product_id).strip():
            continue
        query_id = choose(row, mapping["query_id"])
        relevance_value = choose(row, mapping["relevance"])
        rank_value = choose(row, mapping["rank"])
        item = {
            "query_id": str(query_id).strip() if query_id is not None else None,
            "product_id": str(product_id).strip(),
            "relevance": parse_number(relevance_value) if relevance_value is not None else 0.0,
            "rank": parse_number(rank_value) if rank_value is not None else float(len(normalized) + 1),
        }
        key = (item["query_id"] or "", item["product_id"])
        if key in seen:
            duplicates += 1
            previous = normalized[seen[key]]
            if (item["relevance"], -item["rank"]) > (previous["relevance"], -previous["rank"]):
                normalized[seen[key]] = item
            continue
        seen[key] = len(normalized)
        normalized.append(item)
    return normalized, duplicates


def dcg(relevances: list[float]) -> float:
    return sum((2**rel - 1) / math.log2(index + 2) for index, rel in enumerate(relevances))


def ranking_metrics(records: list[dict[str, Any]]) -> dict[str, float | int | None]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if row["query_id"] is not None:
            grouped[row["query_id"]].append(row)
    if not grouped:
        return {
            "query_count": 0,
            "recall_at_1": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "mrr": None,
            "ndcg_at_10": None,
        }
    recalls = {1: [], 5: [], 10: []}
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for rows in grouped.values():
        ranked = sorted(rows, key=lambda row: row["rank"])
        relevant = [row for row in ranked if row["relevance"] > 0]
        relevant_ids = {row["product_id"] for row in relevant}
        for k, values in recalls.items():
            values.append(1.0 if relevant_ids & {row["product_id"] for row in ranked[:k]} else 0.0)
        first_rank = next((index + 1 for index, row in enumerate(ranked) if row["relevance"] > 0), None)
        reciprocal_ranks.append(1.0 / first_rank if first_rank is not None else 0.0)
        observed = [max(row["relevance"], 0.0) for row in ranked[:10]]
        ideal = sorted((max(row["relevance"], 0.0) for row in ranked), reverse=True)[:10]
        ideal_dcg = dcg(ideal)
        ndcgs.append(dcg(observed) / ideal_dcg if ideal_dcg else 0.0)
    return {
        "query_count": len(grouped),
        "recall_at_1": round(sum(recalls[1]) / len(recalls[1]), 6),
        "recall_at_5": round(sum(recalls[5]) / len(recalls[5]), 6),
        "recall_at_10": round(sum(recalls[10]) / len(recalls[10]), 6),
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6),
        "ndcg_at_10": round(sum(ndcgs) / len(ndcgs), 6),
    }


def report_for_source(manifest: dict[str, Any], source_id: str, input_path: Path | None, mapping_path: Path | None, repo: Path) -> dict[str, Any]:
    source = next((item for item in manifest["sources"] if item["source_id"] == source_id), None)
    if source is None:
        raise ValueError(f"unknown source_id: {source_id}")
    report: dict[str, Any] = {
        "evaluation": "public_product_data_baseline",
        "source_id": source_id,
        "revision": source["revision"],
        "license_status": source["license_status"],
        "input_sha256": None,
        "pipeline_version": PIPELINE_VERSION,
        "real_service_acceptance": False,
        "status": "NOT_RUN",
        "metrics": {
            "records_read": 0,
            "records_clean": 0,
            "duplicate_records_removed": 0,
            "unique_products": 0,
            "query_count": 0,
            "recall_at_1": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "mrr": None,
            "ndcg_at_10": None,
        },
        "input_integrity": {"status": "NOT_RUN", "issues": []},
        "notes": ["No network, model, synthetic business cases, merchant policy, permission, or customer-reply truth is used."],
    }
    ready = (
        source["download_status"] == "downloaded"
        and source["revision"]
        and str(source["license_status"]).startswith("verified")
        and source["original_source_terms_status"] == "verified"
    )
    if not ready:
        report["notes"].append("Source revision/license/terms/download are not verified; public track remains NOT_RUN.")
        return report
    if input_path is None:
        report["notes"].append("No restricted input file was supplied.")
        return report
    resolved = input_path.resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("raw public input must be outside the repository")
    if not resolved.is_file():
        raise ValueError(f"input file does not exist: {resolved}")
    if source["actual_filename"] and resolved.name != source["actual_filename"]:
        raise ValueError(f"input filename does not match manifest actual_filename: {resolved.name}")
    input_sha = file_sha256(resolved)
    report["input_sha256"] = input_sha
    if input_sha != source["sha256"]:
        report["status"] = "FAIL"
        report["input_integrity"] = {"status": "FAIL", "issues": ["sha256_mismatch"]}
        return report
    mapping = DEFAULT_MAPPING
    if mapping_path is not None:
        supplied = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("mapping must be a JSON object")
        mapping = {key: [str(value)] if isinstance(value, str) else value for key, value in {**DEFAULT_MAPPING, **supplied}.items()}
        if any(key not in mapping or not isinstance(mapping[key], list) or not mapping[key] for key in DEFAULT_MAPPING):
            raise ValueError("mapping must provide non-empty field name lists")
    raw_records = load_records(resolved)
    records, duplicates = normalize_records(raw_records, mapping)
    products = {row["product_id"] for row in records}
    report["status"] = "PASS"
    report["metrics"] = {"records_read": len(raw_records), "records_clean": len(records), "duplicate_records_removed": duplicates, "unique_products": len(products), **ranking_metrics(records)}
    report["input_integrity"] = {"status": "PASS", "issues": []}
    report["notes"].append("Metrics use relevance labels and rank/order in the restricted public input; they are not customer-answer or real-service acceptance metrics.")
    return report


def report_hash(report: dict[str, Any]) -> str:
    stable = json.loads(json.dumps(report, ensure_ascii=False))
    stable.pop("report_sha256", None)
    return stable_hash(stable)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--manifest", type=Path, default=root / "public-data-sources.manifest.json")
    parser.add_argument("--source-id", default="amazon-esci")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--mapping", type=Path, help="JSON field mapping for a restricted input")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        report = report_for_source(manifest, args.source_id, args.input, args.mapping, root.parent)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL {exc}")
        return 1
    report["report_sha256"] = report_hash(report)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
