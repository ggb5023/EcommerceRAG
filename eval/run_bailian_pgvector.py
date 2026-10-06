#!/usr/bin/env python3
"""Run an opt-in Alibaba Bailian embedding -> isolated pgvector evaluation.

The default path validates inputs only.  ``--live`` requires an explicit
restricted environment file whose database name starts with ``ecr_m1_test_``
and whose user is ``rag_app``.  Embeddings are loaded into PostgreSQL TEMP
tables inside one transaction and the transaction is rolled back.  Reports
contain IDs, ranks, hashes and counts only; they never contain vectors,
document text, prompts, credentials or customer data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))
EVAL_ROOT = ROOT / "eval"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from app.providers import build_provider
from app.providers.config import ProviderConfigError, load_provider_config
from app.providers.contracts import EmbeddingResult, ProviderError
from run_bailian_retrieval import (
    DEFAULT_ALIGNMENT,
    DEFAULT_CASES,
    DEFAULT_CORPUS,
    DEFAULT_METADATA,
    _eligible_chunks,
    _expected_actual_ids,
    load_inputs,
    select_cases,
    sha256,
)

DEFAULT_DB_ENV = Path("/etc/ecommerce-rag/m1-review.env")
DB_NAME_RE = re.compile(r"^ecr_m1_test_[A-Za-z0-9_]+$")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("db_env_invalid_line")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in values:
            raise ValueError("db_env_duplicate_or_empty_key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def validate_isolated_db(values: Mapping[str, str]) -> list[str]:
    issues: list[str] = []
    database = values.get("PGDATABASE", "")
    user = values.get("PGUSER", "")
    if not DB_NAME_RE.fullmatch(database):
        issues.append("database_must_be_isolated_m1_test")
    if user != "rag_app":
        issues.append("database_user_must_be_rag_app")
    if values.get("PGHOST", "") == "":
        issues.append("database_host_missing")
    if values.get("PGPASSWORD", "") == "":
        issues.append("database_password_missing")
    return issues


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _vector_literal(values: Sequence[float]) -> str:
    if len(values) != 1024 or not all(math.isfinite(float(value)) for value in values):
        raise ValueError("embedding_vector_must_be_1024_finite_values")
    return "[" + ",".join(format(float(value), ".9g") for value in values) + "]"


def _date_literal(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("business_date_invalid")
    date.fromisoformat(value)
    return _sql_literal(value)


def build_sql(
    chunks: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    embeddings: Sequence[EmbeddingResult],
    query_embeddings: Sequence[EmbeddingResult],
) -> str:
    if len(chunks) != len(embeddings) or len(queries) != len(query_embeddings):
        raise ValueError("embedding_sequence_length_mismatch")
    lines = [
        "BEGIN;",
        "CREATE TEMP TABLE provider_eval_chunks (",
        "  chunk_id text PRIMARY KEY, document_id text NOT NULL, tenant_id text NOT NULL,",
        "  shop_id text NOT NULL, disclosure_class text NOT NULL, effective_from date,",
        "  effective_to date, embedding halfvec(1024) NOT NULL",
        ") ON COMMIT DROP;",
        "CREATE TEMP TABLE provider_eval_queries (",
        "  case_id text PRIMARY KEY, tenant_id text NOT NULL, shop_id text NOT NULL,",
        "  business_date date NOT NULL, embedding halfvec(1024) NOT NULL",
        ") ON COMMIT DROP;",
    ]
    for chunk, embedding in zip(chunks, embeddings):
        disclosure = chunk.get("disclosure_class")
        if disclosure != "external_allowed":
            raise ValueError("non_external_chunk_passed_to_db")
        lines.append(
            "INSERT INTO provider_eval_chunks "
            "(chunk_id, document_id, tenant_id, shop_id, disclosure_class, effective_from, effective_to, embedding) VALUES ("
            + ", ".join(
                (
                    _sql_literal(chunk.get("chunk_id")),
                    _sql_literal(chunk.get("document_id")),
                    _sql_literal(chunk.get("tenant_id")),
                    _sql_literal(chunk.get("shop_id")),
                    _sql_literal(disclosure),
                    _date_literal(chunk.get("effective_from")) if chunk.get("effective_from") else "NULL",
                    _date_literal(chunk.get("effective_to")) if chunk.get("effective_to") else "NULL",
                    _sql_literal(_vector_literal(embedding.dense)),
                )
            )
            + ");"
        )
    for case, embedding in zip(queries, query_embeddings):
        authorization = case.get("authorization")
        if not isinstance(authorization, Mapping):
            raise TypeError("authorization_invalid")
        lines.append(
            "INSERT INTO provider_eval_queries "
            "(case_id, tenant_id, shop_id, business_date, embedding) VALUES ("
            + ", ".join(
                (
                    _sql_literal(case.get("case_id")),
                    _sql_literal(authorization.get("tenant_id")),
                    _sql_literal(authorization.get("shop_id")),
                    _date_literal(case.get("business_date")),
                    _sql_literal(_vector_literal(embedding.dense)),
                )
            )
            + ");"
        )
    lines.extend(
        [
            "SELECT q.case_id, COALESCE(string_agg(r.document_id || ':' || r.chunk_id || ':' || r.rank::text, ',' ORDER BY r.rank), '')",
            "FROM provider_eval_queries q",
            "LEFT JOIN LATERAL (",
            "  SELECT c.document_id, c.chunk_id, row_number() OVER (ORDER BY c.embedding <=> q.embedding, c.chunk_id) AS rank",
            "  FROM provider_eval_chunks c",
            "  WHERE c.tenant_id = q.tenant_id AND c.shop_id = q.shop_id",
            "    AND c.disclosure_class = 'external_allowed'",
            "    AND (c.effective_from IS NULL OR c.effective_from <= q.business_date)",
            "    AND (c.effective_to IS NULL OR c.effective_to > q.business_date)",
            "  ORDER BY c.embedding <=> q.embedding, c.chunk_id",
            "  LIMIT 5",
            ") r ON TRUE",
            "GROUP BY q.case_id ORDER BY q.case_id;",
            "ROLLBACK;",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_rank_rows(stdout: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line == "ROLLBACK":
            continue
        case_id, _, packed = line.partition("\t")
        if not case_id or case_id in result:
            raise ValueError("db_output_invalid")
        rows: list[dict[str, Any]] = []
        if packed:
            for item in packed.split(","):
                document_id, chunk_id, rank_text = item.split(":", 2)
                rows.append({"document_id": document_id, "chunk_id": chunk_id, "rank": int(rank_text)})
        result[case_id] = rows
    return result


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _report_base(cases_path: Path, corpus_path: Path, alignment_path: Path, issues: list[str]) -> dict[str, Any]:
    return {
        "report_version": "bailian-pgvector-v1",
        "provider_profile": "aliyun-bailian",
        "input_sha256": sha256(cases_path) if cases_path.is_file() else None,
        "corpus_sha256": sha256(corpus_path) if corpus_path.is_file() else None,
        "alignment_sha256": sha256(alignment_path) if alignment_path.is_file() else None,
        "status": "NOT_RUN",
        "issues": sorted(set(issues)),
        "online_requests_made": False,
        "database_requests_made": False,
        "transaction_rolled_back": False,
        "real_service_acceptance": False,
        "m1_connected": False,
        "model_quality_claim": False,
        "case_results": [],
    }


def _expected_hit(rows: Sequence[Mapping[str, Any]], expected_ids: set[str]) -> bool:
    return any(row.get("document_id") in expected_ids for row in rows[:5])


async def run_live(
    provider: Any,
    cases: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    alignment: Mapping[str, Any],
    db_values: Mapping[str, str],
    *,
    psql_runner: Any = subprocess.run,
) -> dict[str, Any]:
    selected_chunks: list[dict[str, Any]] = []
    for case in cases:
        for chunk in _eligible_chunks(case, corpus):
            if chunk not in selected_chunks:
                selected_chunks.append(chunk)
    if not selected_chunks:
        return {"status": "FAIL", "issues": ["no_eligible_chunks"], "case_results": []}
    document_embeddings: list[EmbeddingResult] = []
    try:
        for start in range(0, len(selected_chunks), 16):
            document_embeddings.extend(
                await provider.embed(
                    [str(chunk.get("content", "")) for chunk in selected_chunks[start : start + 16]],
                    text_type="document",
                    dimensions=1024,
                    output_type="dense&sparse",
                )
            )
        query_embeddings: list[EmbeddingResult] = []
        for case in cases:
            query_embeddings.extend(
                await provider.embed(
                    [str(case.get("query", ""))],
                    text_type="query",
                    dimensions=1024,
                    output_type="dense&sparse",
                )
            )
    except ProviderError as error:
        return {"status": "FAIL", "issues": [f"embedding:{error.code}"], "case_results": []}
    sql = build_sql(selected_chunks, cases, document_embeddings, query_embeddings)
    env = {"PATH": os.environ.get("PATH", ""), **{key: value for key, value in db_values.items()}}
    try:
        completed = psql_runner(
            ["psql", "-X", "-q", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"],
            input=sql,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError:
        return {"status": "NOT_RUN", "issues": ["psql_unavailable"], "case_results": []}
    if completed.returncode != 0:
        return {
            "status": "FAIL",
            "issues": ["database_query_failed"],
            "database_error_output_saved": False,
            "case_results": [],
        }
    try:
        ranked = parse_rank_rows(completed.stdout)
    except (ValueError, IndexError):
        return {"status": "FAIL", "issues": ["database_output_invalid"], "case_results": []}
    case_results: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        rows = ranked.get(case_id, [])
        expected_ids = _expected_actual_ids(case, alignment)
        case_results.append(
            {
                "case_id": case_id,
                "expected_document_ids": sorted(expected_ids),
                "top5": rows,
                "embedding_hit_at_5": _expected_hit(rows, expected_ids),
                "tenant_shop_date_disclosure_filters": True,
            }
        )
    return {
        "status": "PASS" if len(ranked) == len(cases) else "FAIL",
        "issues": [] if len(ranked) == len(cases) else ["database_case_output_incomplete"],
        "case_results": case_results,
        "candidate_count": len(selected_chunks),
        "embedding_dimension": 1024,
        "transaction_rolled_back": True,
        "database_requests_made": True,
    }


async def main_async(args: argparse.Namespace) -> int:
    cases, corpus, alignment, input_issues = load_inputs(
        args.cases, args.corpus, args.alignment, args.metadata
    )
    report = _report_base(args.cases, args.corpus, args.alignment, input_issues)
    if input_issues or not args.live:
        if not args.live:
            report["issues"].append("live_flag_required")
        print(json.dumps(report, ensure_ascii=False))
        return 0
    try:
        selected = select_cases(cases, args.limit)
        db_values = parse_env_file(args.db_env)
        db_issues = validate_isolated_db(db_values)
        if db_issues:
            report["issues"].extend(db_issues)
            report["status"] = "CONFIG_BLOCKED"
            print(json.dumps(report, ensure_ascii=False))
            return 3
        config = load_provider_config(args.env)
        if config.profile != "aliyun-bailian":
            raise ProviderConfigError("provider profile is not aliyun-bailian")
        run = await run_live(build_provider(config), selected, corpus, alignment, db_values)
    except (OSError, ValueError, ProviderConfigError) as error:
        report["issues"].append(f"configuration:{type(error).__name__}")
        report["status"] = "CONFIG_BLOCKED"
        print(json.dumps(report, ensure_ascii=False))
        return 3
    report.update(run)
    report["case_count"] = len(selected)
    report["online_requests_made"] = True
    report["database_name_fingerprint"] = _fingerprint(db_values["PGDATABASE"])
    report["database_user"] = db_values["PGUSER"]
    report["database_scope"] = "isolated_m1_test"
    report["real_service_acceptance"] = False
    report["m1_connected"] = False
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(args.output, 0o600)
    print(json.dumps({key: report.get(key) for key in ("status", "case_count", "candidate_count", "transaction_rolled_back", "real_service_acceptance")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--env", type=Path, default=Path("/etc/ecommerce-rag/providers.env"))
    parser.add_argument("--db-env", type=Path, default=DEFAULT_DB_ENV)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--alignment", type=Path, default=DEFAULT_ALIGNMENT)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
