from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

import approve_alignment


ROOT = Path(__file__).parents[1]
PROVISIONAL = Path("/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional")


def test_unresolved_review_cannot_be_approved() -> None:
    proposal_path = PROVISIONAL / "alignment-proposal.json"
    review_path = PROVISIONAL / "alignment-review.json"
    corpus_path = PROVISIONAL / "documents.jsonl"
    with pytest.raises(ValueError, match="all mapping rows must be approved"):
        approve_alignment.build_approved_alignment(
            json.loads(proposal_path.read_text()),
            proposal_path,
            json.loads(review_path.read_text()),
            review_path,
            corpus_path,
        )


def test_complete_approval_preserves_evidence(tmp_path: Path) -> None:
    proposal = {
        "status": "PENDING_REVIEW",
        "real_service_acceptance": False,
        "alignment_version": "test-v1",
        "eval_set_version": "test-v1",
        "cases_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "case_to_source_documents": {"case-1": {"label": "doc-1"}},
        "case_checks": [{
            "case_id": "case-1",
            "expected_doc_ids": ["label"],
            "document_checks": [{"source_document_id": "doc-1", "evidence_chunks": []}],
        }],
    }
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    review = {
        "proposal_sha256": hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
        "real_service_acceptance": False,
        "case_count": 1,
        "review_status_counts": {"approved": 1},
        "rows": [{
            "case_id": "case-1",
            "expected_doc_ids": ["label"],
            "source_document_ids": ["doc-1"],
            "document_checks": [{"source_document_id": "doc-1", "evidence_chunks": []}],
            "review_status": "approved",
            "review_notes": "",
        }],
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text("{}\n", encoding="utf-8")
    result = approve_alignment.build_approved_alignment(proposal, proposal_path, review, review_path, corpus_path)
    assert result["status"] == "APPROVED"
    assert result["case_evidence"]["case-1"][0]["source_document_id"] == "doc-1"


def test_cli_writes_new_file_and_refuses_overwrite(tmp_path: Path) -> None:
    proposal = {
        "status": "PENDING_REVIEW",
        "real_service_acceptance": False,
        "alignment_version": "test-v1",
        "eval_set_version": "test-v1",
        "cases_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "case_to_source_documents": {"case-1": {"label": "doc-1"}},
        "case_checks": [{"case_id": "case-1", "expected_doc_ids": ["label"], "document_checks": [{"source_document_id": "doc-1"}]}],
    }
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    review = {
        "proposal_sha256": hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
        "real_service_acceptance": False,
        "case_count": 1,
        "review_status_counts": {"approved": 1},
        "rows": [{
            "case_id": "case-1", "expected_doc_ids": ["label"],
            "source_document_ids": ["doc-1"], "document_checks": [{"source_document_id": "doc-1"}],
            "review_status": "approved", "review_notes": "",
        }],
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "approved.json"
    import subprocess
    import sys

    command = [
        sys.executable, str(Path(approve_alignment.__file__)),
        "--proposal", str(proposal_path), "--review", str(review_path),
        "--corpus", str(corpus_path), "--output", str(output), "--approve",
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0
    approved = json.loads(output.read_text(encoding="utf-8"))
    assert approved["status"] == "APPROVED"
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode != 0
    assert "refusing to overwrite" in second.stderr
