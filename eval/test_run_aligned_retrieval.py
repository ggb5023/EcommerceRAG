from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("run_aligned_retrieval.py")


def _run(
    tmp_path: Path,
    alignment: dict,
    *,
    case: dict | None = None,
    chunk: dict | None = None,
) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cases = [case or {
        "case_id": "case-1",
        "query": "蓝色收纳箱",
        "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["有资料支持"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }]
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    corpus_path = tmp_path / "documents.jsonl"
    corpus_path.write_text(
        json.dumps({
            "document_id": "actual-doc-v1",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "chunks": [chunk or {
                "chunk_id": "chunk-1",
                "content": "蓝色收纳箱",
                "tenant_id": "tenant-a",
                "shop_id": "shop-a",
                "disclosure_class": "external_allowed",
                "effective_from": "2026-01-01",
                "effective_to": None,
            }],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text(json.dumps(alignment), encoding="utf-8")
    report_path = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cases",
            str(cases_path),
            "--corpus",
            str(corpus_path),
            "--alignment",
            str(alignment_path),
            "--output",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_pending_alignment_never_reports_metrics(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        {
            "status": "PENDING_REVIEW",
            "case_to_source_documents": {},
            "real_service_acceptance": False,
        },
    )
    assert report["status"] == "NOT_RUN"
    assert report["metrics"]["recall_at_5"] is None
    assert report["status_counts"] == {"NOT_RUN": 1}
    assert "alignment_pending_review" in report["issues"]
    assert "alignment_mapping_empty" in report["issues"]


def test_explicit_mapping_measures_actual_source_document(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        {
            "status": "APPROVED",
            "case_to_source_documents": {"case-1": {"label-doc": "actual-doc-v1"}},
            "real_service_acceptance": False,
        },
    )
    assert report["status"] == "PASS"
    assert report["measured_case_count"] == 1
    assert report["metrics"]["recall_at_5"] == 1.0


def test_approved_empty_mapping_stays_not_run(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        {
            "status": "APPROVED",
            "case_to_source_documents": {},
            "real_service_acceptance": False,
        },
    )
    assert report["status"] == "NOT_RUN"
    assert report["measured_case_count"] == 0
    assert report["metrics"] == {"recall_at_5": None, "mrr": None, "ndcg_at_5": None}
    assert "alignment_mapping_empty" in report["issues"]
    assert report["case_results"][0]["reason"] == "missing_alignment"


def test_expected_document_drift_is_rejected(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        {
            "status": "APPROVED",
            "case_to_source_documents": {"case-1": {"other-label": "actual-doc-v1"}},
            "real_service_acceptance": False,
        },
    )
    assert report["status"] == "NOT_RUN"
    assert report["status_counts"] == {"ALIGNMENT_INVALID": 1}


def test_duplicate_corpus_document_ids_are_not_overwritten(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({
        "case_id": "case-1", "query": "蓝色收纳箱", "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["有资料支持"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    corpus = tmp_path / "documents.jsonl"
    document = {"document_id": "actual-doc", "tenant_id": "tenant-a", "shop_id": "shop-a", "chunks": []}
    corpus.write_text(json.dumps(document) + "\n" + json.dumps(document) + "\n", encoding="utf-8")
    alignment = tmp_path / "alignment.json"
    alignment.write_text(json.dumps({"status": "APPROVED", "real_service_acceptance": False,
                                    "case_to_source_documents": {"case-1": {"label-doc": "actual-doc"}}}),
                         encoding="utf-8")
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(SCRIPT), "--cases", str(cases), "--corpus", str(corpus),
                    "--alignment", str(alignment), "--output", str(output)], check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_RUN"
    assert report["metrics"]["recall_at_5"] is None
    assert any(issue.startswith("duplicate_corpus_document_id:") for issue in report["issues"])


def test_document_scope_mismatch_is_rejected(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({
        "case_id": "case-1", "query": "蓝色收纳箱", "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["有资料支持"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    corpus = tmp_path / "documents.jsonl"
    corpus.write_text(json.dumps({"document_id": "actual-doc", "tenant_id": "tenant-b", "shop_id": "shop-b",
                                  "chunks": []}) + "\n", encoding="utf-8")
    alignment = tmp_path / "alignment.json"
    alignment.write_text(json.dumps({"status": "APPROVED", "real_service_acceptance": False,
                                    "case_to_source_documents": {"case-1": {"label-doc": "actual-doc"}}}),
                         encoding="utf-8")
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(SCRIPT), "--cases", str(cases), "--corpus", str(corpus),
                    "--alignment", str(alignment), "--output", str(output)], check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_RUN"
    assert report["case_results"][0]["reason"] == "document_scope_mismatch"


def test_multiple_expected_ids_cannot_alias_one_source_document(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({
        "case_id": "case-1", "query": "比较商品", "expected_doc_ids": ["label-a", "label-b"],
        "expected_answer_points": ["可比较"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    corpus = tmp_path / "documents.jsonl"
    corpus.write_text(json.dumps({"document_id": "actual-doc", "tenant_id": "tenant-a", "shop_id": "shop-a",
                                  "chunks": []}) + "\n", encoding="utf-8")
    alignment = tmp_path / "alignment.json"
    alignment.write_text(json.dumps({"status": "APPROVED", "real_service_acceptance": False,
                                    "case_to_source_documents": {"case-1": {"label-a": "actual-doc", "label-b": "actual-doc"}}}),
                         encoding="utf-8")
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(SCRIPT), "--cases", str(cases), "--corpus", str(corpus),
                    "--alignment", str(alignment), "--output", str(output)], check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_RUN"
    assert report["case_results"][0]["reason"] == "source_document_alias"


def _policy_case(*, tags: list[str] | None = None) -> dict:
    return {
        "case_id": "case-1",
        "query": "内部资料",
        "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["按权限处理"],
        "tags": tags or [],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }


def _policy_alignment() -> dict:
    return {
        "status": "APPROVED",
        "case_to_source_documents": {"case-1": {"label-doc": "actual-doc-v1"}},
        "real_service_acceptance": False,
    }


def test_expected_policy_refusal_is_counted_without_polluting_metrics(tmp_path: Path) -> None:
    report = _run(tmp_path, _policy_alignment(), case=_policy_case(tags=["unauthorized"]), chunk={
        "chunk_id": "chunk-1", "content": "内部资料", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "internal_only", "effective_from": "2026-01-01", "effective_to": None,
    })
    assert report["status"] == "PASS"
    assert report["metrics"] == {"recall_at_5": None, "mrr": None, "ndcg_at_5": None}
    assert report["measured_case_count"] == 0
    assert report["refusal_counts"]["matched"] == 1
    assert report["case_results"][0]["status"] == "ACCESS_DENIED"


def test_unexpected_policy_refusal_cannot_count_as_retrieval_success(tmp_path: Path) -> None:
    report = _run(tmp_path, _policy_alignment(), chunk={
        "chunk_id": "chunk-1", "content": "内部资料", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "internal_only", "effective_from": "2026-01-01", "effective_to": None,
    })
    assert report["status"] == "NOT_RUN"
    assert report["metrics"] == {"recall_at_5": None, "mrr": None, "ndcg_at_5": None}
    assert report["refusal_counts"]["unmatched"] == 1
    assert "case-1:unexpected_policy_refusal:ACCESS_DENIED" in report["issues"]


def test_unclassified_source_is_blocked_for_operator(tmp_path: Path) -> None:
    report = _run(tmp_path, _policy_alignment(), case=_policy_case(tags=["unanswerable"]), chunk={
        "chunk_id": "chunk-1", "content": "未分类资料", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "unclassified", "effective_from": "2026-01-01", "effective_to": None,
    })
    assert report["case_results"][0]["status"] == "UNCLASSIFIED_BLOCKED"
    assert report["status"] == "PASS"


def test_expired_and_future_sources_are_distinguished(tmp_path: Path) -> None:
    expired = _run(tmp_path / "expired", _policy_alignment(), case=_policy_case(tags=["unanswerable"]), chunk={
        "chunk_id": "chunk-1", "content": "旧资料", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "external_allowed", "effective_from": "2026-01-01", "effective_to": "2026-10-01",
    })
    future = _run(tmp_path / "future", _policy_alignment(), case={**_policy_case(tags=["unanswerable"]), "business_date": "2025-12-01"}, chunk={
        "chunk_id": "chunk-1", "content": "未来资料", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "external_allowed", "effective_from": "2026-01-01", "effective_to": None,
    })
    assert expired["case_results"][0]["status"] == "EXPIRED_OR_REVOKED"
    assert future["case_results"][0]["status"] == "NOT_YET_EFFECTIVE"
    assert expired["status"] == future["status"] == "PASS"
