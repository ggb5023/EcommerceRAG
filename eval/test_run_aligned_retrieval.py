from __future__ import annotations

import hashlib
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
    had_evidence = "case_evidence" in alignment
    if alignment.get("status") == "APPROVED" and had_evidence and "source_corpus_sha256" not in alignment:
        source_id = next(iter((alignment.get("case_to_source_documents") or {}).get(cases[0]["case_id"], {}).values()), "actual-doc-v1")
        alignment["case_evidence"] = {
            case["case_id"]: [{"source_document_id": source_id, "evidence_chunks": [{"chunk_id": "chunk-1", "source_position": {"line_start": 1}}]}]
            for case in cases
        }
    if alignment.get("status") == "APPROVED" and had_evidence:
        alignment["source_corpus_sha256"] = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
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
            "source_corpus_sha256": "0" * 64,
            "case_evidence": {"case-1": [{"source_document_id": "actual-doc-v1", "evidence_chunks": [{"chunk_id": "chunk-1", "source_position": {"line_start": 1}}]}]},
        },
    )
    assert report["status"] == "PASS"
    assert report["measured_case_count"] == 1
    assert report["metrics"]["recall_at_5"] == 1.0


def test_tampered_approved_evidence_never_measures(tmp_path: Path) -> None:
    report = _run(tmp_path, {
        "status": "APPROVED",
        "case_to_source_documents": {"case-1": {"label-doc": "actual-doc-v1"}},
        "real_service_acceptance": False,
        "source_corpus_sha256": "0" * 64,
        "case_evidence": {},
    })
    assert report["status"] == "NOT_RUN"
    assert report["measured_case_count"] == 0
    assert "approved_case_evidence_case_set_mismatch" in report["issues"]


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


def test_revoked_answer_point_marks_expired_refusal_as_expected(tmp_path: Path) -> None:
    report = _run(tmp_path, _policy_alignment(), case={**_policy_case(tags=["policy", "negative"]), "expected_answer_points": ["撤销版本不能继续使用"]}, chunk={
        "chunk_id": "chunk-1", "content": "撤销版本不能继续使用", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "disclosure_class": "external_allowed", "effective_from": "2026-01-01", "effective_to": "2026-10-01",
    })
    assert report["case_results"][0]["status"] == "EXPIRED_OR_REVOKED"
    assert report["case_results"][0]["refusal_match"] is True
    assert report["status"] == "PASS"


def test_document_ranking_deduplicates_chunks_before_ndcg(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps({
        "case_id": "case-1", "query": "蓝色 收纳箱", "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["有资料支持"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    corpus_path = tmp_path / "documents.jsonl"
    corpus_path.write_text(json.dumps({
        "document_id": "actual-doc-v1", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "chunks": [
            {"chunk_id": "chunk-a", "content": "蓝色 收纳箱", "tenant_id": "tenant-a", "shop_id": "shop-a", "disclosure_class": "external_allowed", "effective_from": "2026-01-01", "effective_to": None},
            {"chunk_id": "chunk-b", "content": "蓝色 收纳箱 规格", "tenant_id": "tenant-a", "shop_id": "shop-a", "disclosure_class": "external_allowed", "effective_from": "2026-01-01", "effective_to": None},
        ],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text(json.dumps({
        "status": "APPROVED", "real_service_acceptance": False,
        "case_to_source_documents": {"case-1": {"label-doc": "actual-doc-v1"}},
    }), encoding="utf-8")
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(SCRIPT), "--cases", str(cases_path), "--corpus", str(corpus_path), "--alignment", str(alignment_path), "--output", str(output)], check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["case_results"][0]["result_document_ids"] == ["actual-doc-v1"]
    assert report["metrics"]["ndcg_at_5"] == 1.0


def _run_input_failure(
    tmp_path: Path,
    *,
    cases: bytes | None = None,
    corpus: bytes | None = None,
    metadata: bytes | None = None,
    alignment: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cases_path = tmp_path / "cases.jsonl"
    corpus_path = tmp_path / "documents.jsonl"
    metadata_path = tmp_path / "metadata.json"
    alignment_path = tmp_path / "alignment.json"
    cases_path.write_bytes(cases or b'{"case_id":"case-1","query":"q","expected_doc_ids":["d"],"expected_answer_points":["p"],"authorization":{"tenant_id":"t","shop_id":"s","role":"operator"},"business_date":"2026-10-04"}\n')
    corpus_path.write_bytes(corpus or b'{"document_id":"d","tenant_id":"t","shop_id":"s","chunks":[]}\n')
    metadata_path.write_bytes(metadata or b'{"eval_set_version":"test-v1"}')
    alignment_path.write_bytes(alignment or b'{"status":"PENDING_REVIEW","case_to_source_documents":{},"real_service_acceptance":false}')
    output = tmp_path / "report.json"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cases",
            str(cases_path),
            "--corpus",
            str(corpus_path),
            "--metadata",
            str(metadata_path),
            "--alignment",
            str(alignment_path),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_malformed_cases_fail_input_integrity_without_report(tmp_path: Path) -> None:
    result = _run_input_failure(tmp_path, cases=b'{"case_id":"a","case_id":"b"}\n')
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL input_integrity:")
    assert "duplicate JSON key" in result.stdout
    assert not (tmp_path / "report.json").exists()


def test_invalid_utf8_corpus_fails_input_integrity_without_report(tmp_path: Path) -> None:
    result = _run_input_failure(tmp_path, corpus=b"{\xff\n")
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL input_integrity:")
    assert "not valid UTF-8" in result.stdout
    assert not (tmp_path / "report.json").exists()


def test_non_object_corpus_row_fails_input_integrity_without_report(tmp_path: Path) -> None:
    result = _run_input_failure(tmp_path, corpus=b"[]\n")
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL input_integrity:")
    assert "must be a JSON object" in result.stdout
    assert not (tmp_path / "report.json").exists()


def test_malformed_metadata_and_alignment_fail_input_integrity(tmp_path: Path) -> None:
    metadata_result = _run_input_failure(tmp_path / "metadata", metadata=b'{"eval_set_version":"a","eval_set_version":"b"}')
    assert metadata_result.returncode == 1
    assert "duplicate JSON key" in metadata_result.stdout
    alignment_result = _run_input_failure(tmp_path / "alignment", alignment=b"[]")
    assert alignment_result.returncode == 1
    assert "alignment must be a JSON object" in alignment_result.stdout


def test_revision_root_selects_approved_revision_inputs(tmp_path: Path) -> None:
    revision_root = tmp_path / "approved-revision"
    revision_root.mkdir()
    case = {
        "case_id": "case-1",
        "query": "蓝色收纳箱",
        "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["有资料支持"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        "business_date": "2026-10-04",
    }
    cases_path = revision_root / "synthetic-m2-v1-revision-1.jsonl"
    cases_path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    (revision_root / "synthetic-m2-v1-revision-1.metadata.json").write_text(
        json.dumps({"eval_set_version": "synthetic-test-revision-1"}), encoding="utf-8"
    )
    corpus_path = revision_root / "documents.jsonl"
    corpus_path.write_text(json.dumps({
        "document_id": "actual-doc-v1",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "content": "蓝色收纳箱",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "disclosure_class": "external_allowed",
            "effective_from": "2026-01-01",
            "effective_to": None,
        }],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    alignment = {
        "status": "APPROVED",
        "real_service_acceptance": False,
        "case_to_source_documents": {"case-1": {"label-doc": "actual-doc-v1"}},
        "case_evidence": {"case-1": [{
            "source_document_id": "actual-doc-v1",
            "evidence_chunks": [{"chunk_id": "chunk-1", "source_position": {"line_start": 1}}],
        }]},
        "source_corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
    }
    (revision_root / "alignment-revision-1.json").write_text(json.dumps(alignment), encoding="utf-8")
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--revision-root", str(revision_root), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert result.stdout
    assert report["status"] == "PASS"
    assert report["eval_set_version"] == "synthetic-test-revision-1"
    assert report["measured_case_count"] == 1
