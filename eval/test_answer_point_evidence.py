import hashlib
import json
from pathlib import Path

from answer_point_evidence import load_json, validate_review


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, status: str = "APPROVED", review_status: str = "approved"):
    cases_path = tmp_path / "cases.jsonl"
    corpus_path = tmp_path / "corpus.jsonl"
    alignment_path = tmp_path / "alignment.json"
    review_path = tmp_path / "review.json"
    cases = [{"case_id": "case-1", "expected_answer_points": ["事实点"]}]
    corpus = [{
        "document_id": "doc-1",
        "chunks": [{"chunk_id": "chunk-1", "content": "事实点的来源说明"}],
    }]
    alignment = {
        "status": "APPROVED",
        "case_evidence": {"case-1": [{"evidence_chunks": [{"chunk_id": "chunk-1"}]}]},
    }
    cases_path.write_text("{" + '"case_id":"case-1","expected_answer_points":["事实点"]' + "}\n", encoding="utf-8")
    corpus_path.write_text("{" + '"document_id":"doc-1","chunks":[{"chunk_id":"chunk-1","content":"事实点的来源说明"}]' + "}\n", encoding="utf-8")
    alignment_path.write_text(json.dumps(alignment, ensure_ascii=False), encoding="utf-8")
    review = {
        "review_version": "answer-point-evidence-review-v1",
        "status": status,
        "cases_sha256": _sha256(cases_path),
        "alignment_sha256": _sha256(alignment_path),
        "source_corpus_sha256": _sha256(corpus_path),
        "rows": [{
            "case_id": "case-1",
            "point_index": 0,
            "review_status": review_status,
            "support_chunk_ids": ["chunk-1"],
            "support_basis": "semantic_source_review" if review_status == "approved" else "claim_scope_mismatch",
            **({"review_notes_code": "needs_scope_review"} if review_status != "approved" else {}),
        }],
        "real_service_acceptance": False,
    }
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return cases_path, corpus_path, alignment_path, review_path, cases, corpus, alignment


def test_valid_review_returns_point_support_flags(tmp_path):
    paths = _fixture(tmp_path)
    flags, summary = validate_review(*paths[3:4], *paths[:3], *paths[4:])
    assert summary["status"] == "APPROVED"
    assert summary["issues"] == []
    assert summary["approved_point_count"] == 1
    assert flags == {"case-1": [True]}


def test_pending_review_keeps_unresolved_point_false(tmp_path):
    paths = _fixture(tmp_path, status="PENDING_REVIEW", review_status="needs_revision")
    flags, summary = validate_review(*paths[3:4], *paths[:3], *paths[4:])
    assert summary["status"] == "PENDING_REVIEW"
    assert summary["unresolved_point_count"] == 1
    assert flags == {"case-1": [False]}


def test_hash_drift_fails_closed(tmp_path):
    paths = _fixture(tmp_path)
    review_path = paths[3]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["cases_sha256"] = "0" * 64
    review_path.write_text(json.dumps(review), encoding="utf-8")
    flags, summary = validate_review(*paths[3:4], *paths[:3], *paths[4:])
    assert flags == {}
    assert "review_cases_sha256_mismatch" in summary["issues"]


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"status":"PENDING_REVIEW","status":"APPROVED"}', encoding="utf-8")
    try:
        load_json(path)
    except ValueError as error:
        assert "duplicate JSON key" in str(error)
    else:
        raise AssertionError("duplicate key must fail closed")
