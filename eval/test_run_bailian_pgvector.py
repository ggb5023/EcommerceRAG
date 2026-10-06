import asyncio
import importlib.util
import json
import subprocess
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "run_bailian_pgvector", Path(__file__).with_name("run_bailian_pgvector.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_default_report_is_not_run_without_live(tmp_path):
    cases = tmp_path / "cases.jsonl"
    corpus = tmp_path / "corpus.jsonl"
    alignment = tmp_path / "alignment.json"
    cases.write_text("{}\n", encoding="utf-8")
    corpus.write_text("{}\n", encoding="utf-8")
    alignment.write_text("{}", encoding="utf-8")
    report = MODULE._report_base(cases, corpus, alignment, [])
    report["issues"].append("live_flag_required")
    assert report["status"] == "NOT_RUN"
    assert report["online_requests_made"] is False
    assert report["database_requests_made"] is False


def test_database_guard_rejects_shared_database():
    issues = MODULE.validate_isolated_db(
        {"PGDATABASE": "rag", "PGUSER": "rag_app", "PGHOST": "db", "PGPASSWORD": "x"}
    )
    assert issues == ["database_must_be_isolated_m1_test"]


def test_sql_has_scope_date_disclosure_filters_and_rollback():
    class Embedding:
        dense = tuple([0.1] * 1024)

    chunk = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "disclosure_class": "external_allowed",
        "effective_from": "2026-01-01",
        "effective_to": None,
    }
    case = {
        "case_id": "syn-001",
        "business_date": "2026-10-01",
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a"},
    }
    sql = MODULE.build_sql([chunk], [case], [Embedding()], [Embedding()])
    assert "c.tenant_id = q.tenant_id" in sql
    assert "c.disclosure_class = 'external_allowed'" in sql
    assert "c.effective_to > q.business_date" in sql
    assert sql.rstrip().endswith("ROLLBACK;")
    assert "document_version_id text NOT NULL" in sql
    assert "version-1" in sql
    assert "c.document_version_id" in sql
    assert "chunk-1" in sql


def test_parse_rank_rows_is_metadata_only():
    rows = MODULE.parse_rank_rows("syn-001\tdoc-1:version-1:chunk-1:1,doc-2:version-2:chunk-2:2\n")
    assert rows["syn-001"][0] == {
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "chunk_id": "chunk-1",
        "rank": 1,
    }
    assert all("content" not in row for row in rows["syn-001"])


def test_run_live_uses_psql_without_persisting_content():
    class Embedding:
        dense = tuple([0.1] * 1024)

    class Provider:
        async def embed(self, texts, **kwargs):
            return [Embedding() for _ in texts]

    captured = {}

    def fake_psql(args, **kwargs):
        captured["sql"] = kwargs["input"]
        return subprocess.CompletedProcess(args, 0, "case-1\tdoc-1:version-1:chunk-1:1\n", "")

    cases = [{
        "case_id": "case-1",
        "query": "query",
        "expected_answer_points": [],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a"},
        "business_date": "2026-10-01",
    }]
    corpus = [{
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_version_id": "version-1",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "content": "secret source content",
            "disclosure_class": "external_allowed",
            "effective_from": "2026-01-01",
            "effective_to": None,
        }],
    }]
    alignment = {"case_to_source_documents": {"case-1": {"label": "doc-1"}}}
    result = asyncio.run(
        MODULE.run_live(
            Provider(), cases, corpus, alignment,
            {"PGDATABASE": "ecr_m1_test_unit", "PGUSER": "rag_app", "PGHOST": "db", "PGPASSWORD": "x"},
            psql_runner=fake_psql,
        )
    )
    assert result["status"] == "PASS"
    assert result["transaction_rolled_back"] is True
    assert "secret source content" not in captured["sql"]
    assert "secret source content" not in json.dumps(result)


def test_corpus_version_mismatch_is_rejected():
    issues = MODULE.validate_corpus_versions([{
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_version_id": "version-2",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
        }],
    }])
    assert issues == ["corpus_chunk_version_mismatch:chunk-1"]
