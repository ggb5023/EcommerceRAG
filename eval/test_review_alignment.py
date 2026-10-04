from __future__ import annotations

import copy
import json
from pathlib import Path

import review_alignment


def _proposal(tmp_path: Path) -> tuple[Path, dict]:
    proposal = {
        "alignment_version": "test-v1",
        "eval_set_version": "synthetic-m2-v1",
        "case_checks": [{
            "case_id": "case-1",
            "expected_doc_ids": ["label-doc"],
            "document_checks": [{
                "expected_doc_id": "label-doc",
                "source_document_id": "source-doc-v1",
                "scope_match": True,
                "date_state": "active",
                "disclosure_class": "external_allowed",
                "review_required": False,
                "risk_reasons": [],
            }],
        }],
    }
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    return path, proposal


def test_generated_checklist_is_pending_and_separate(tmp_path: Path) -> None:
    path, proposal = _proposal(tmp_path)
    checklist = review_alignment.build_checklist(proposal, path)
    assert checklist["status"] == "PENDING_REVIEW"
    assert checklist["review_status_counts"] == {"pending": 1}
    assert checklist["rows"][0]["source_document_ids"] == ["source-doc-v1"]


def test_approved_checklist_is_reviewed_but_not_alignment_approval(tmp_path: Path) -> None:
    path, proposal = _proposal(tmp_path)
    checklist = review_alignment.build_checklist(proposal, path)
    checklist["rows"][0]["review_status"] = "approved"
    report = review_alignment.validate_checklist(proposal, path, checklist)
    assert report["status"] == "REVIEWED"
    assert report["unresolved_count"] == 0
    assert report["real_service_acceptance"] is False


def test_evidence_tamper_fails_validation(tmp_path: Path) -> None:
    path, proposal = _proposal(tmp_path)
    checklist = review_alignment.build_checklist(proposal, path)
    checklist["rows"][0]["expected_doc_ids"] = ["other-doc"]
    report = review_alignment.validate_checklist(proposal, path, checklist)
    assert report["status"] == "FAIL"
    assert "evidence_drift:case-1:expected_doc_ids" in report["issues"]


def test_revision_requires_notes(tmp_path: Path) -> None:
    path, proposal = _proposal(tmp_path)
    checklist = review_alignment.build_checklist(proposal, path)
    checklist["rows"][0]["review_status"] = "needs_revision"
    report = review_alignment.validate_checklist(proposal, path, checklist)
    assert report["status"] == "FAIL"
    assert "review_notes_missing:case-1" in report["issues"]


def test_invalid_case_id_or_status_fails_without_crashing(tmp_path: Path) -> None:
    path, proposal = _proposal(tmp_path)
    checklist = review_alignment.build_checklist(proposal, path)
    checklist["rows"][0]["case_id"] = {"tampered": True}
    checklist["rows"][0]["review_status"] = {"approved": True}
    report = review_alignment.validate_checklist(proposal, path, checklist)
    assert report["status"] == "FAIL"
    assert "review_case_id_invalid" in report["issues"]
    assert any(issue.startswith("review_status_invalid:") for issue in report["issues"])
