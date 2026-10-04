from __future__ import annotations

import hashlib
import json
from pathlib import Path

import build_alignment_revision_proposal as revision


ROOT = Path(__file__).parents[1]
CASES = ROOT / "eval/synthetic_cases.jsonl"
CORPUS = Path("/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/documents.jsonl")


def test_unresolved_revision_proposal_is_read_only_and_explicit() -> None:
    review_path = CORPUS.parent / "alignment-review.json"
    before = hashlib.sha256(CASES.read_bytes()).hexdigest()
    report = revision.build_proposal(CASES, review_path, CORPUS)
    assert report["status"] == "PENDING_REVIEW"
    assert report["case_count"] == 60
    assert report["unresolved_count"] == 1
    row = report["rows"][0]
    assert row["case_id"] == "syn-005"
    assert row["review_status"] == "needs_revision"
    assert {option["option_id"] for option in row["options"]} == {
        "narrow_claim",
        "add_test_evidence",
        "keep_unresolved",
    }
    assert hashlib.sha256(CASES.read_bytes()).hexdigest() == before


def test_no_unresolved_rows_is_explicit(tmp_path: Path) -> None:
    review = {"rows": [{"case_id": "syn-001", "review_status": "approved", "review_notes": ""}]}
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"case_id": "syn-001", "expected_doc_ids": ["doc"]}) + "\n", encoding="utf-8")
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"document_id": "doc", "text": "source"}) + "\n", encoding="utf-8")
    report = revision.build_proposal(cases, path, corpus)
    assert report["status"] == "NO_UNRESOLVED_ROWS"
    assert report["rows"] == []
