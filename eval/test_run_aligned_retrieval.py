from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("run_aligned_retrieval.py")


def _run(tmp_path: Path, alignment: dict) -> dict:
    cases = [
        {
            "case_id": "case-1",
            "query": "蓝色收纳箱",
            "expected_doc_ids": ["label-doc"],
            "expected_answer_points": ["有资料支持"],
            "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
            "business_date": "2026-10-04",
        }
    ]
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    corpus_path = tmp_path / "documents.jsonl"
    corpus_path.write_text(
        json.dumps(
            {
                "document_id": "actual-doc-v1",
                "tenant_id": "tenant-a",
                "shop_id": "shop-a",
                "chunks": [
                    {
                        "chunk_id": "chunk-1",
                        "content": "蓝色收纳箱",
                        "tenant_id": "tenant-a",
                        "shop_id": "shop-a",
                        "disclosure_class": "external_allowed",
                        "effective_from": "2026-01-01",
                        "effective_to": None,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
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
