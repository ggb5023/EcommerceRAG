from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("build_alignment_proposal.py")
ROOT = SCRIPT.parents[1]
CASES = ROOT / "eval/synthetic_cases.jsonl"
MANIFEST = ROOT / "data/synthetic/ecommerce-m2-aligned-v1/manifest.yaml"


def _run(output: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(CASES), "--manifest", str(MANIFEST), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout
    return json.loads(output.read_text(encoding="utf-8"))


def test_proposal_covers_all_cases_without_approval(tmp_path: Path) -> None:
    proposal = _run(tmp_path / "alignment.json")
    assert proposal["status"] == "PENDING_REVIEW"
    assert proposal["case_count"] == 60
    assert proposal["source_document_count"] == 37
    assert proposal["mapped_case_count"] == 60
    assert proposal["issues"] == []
    assert proposal["real_service_acceptance"] is False
    assert len(proposal["case_to_source_documents"]) == 60


def test_proposal_is_deterministic_and_does_not_change_cases(tmp_path: Path) -> None:
    before = hashlib.sha256(CASES.read_bytes()).hexdigest()
    first = _run(tmp_path / "first.json")
    second = _run(tmp_path / "second.json")
    assert first == second
    assert hashlib.sha256(CASES.read_bytes()).hexdigest() == before
    assert all("query" not in check for check in first["case_checks"])
