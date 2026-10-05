from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import build_alignment_proposal as revision

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


def _run_with_corpus(output: Path, corpus: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(CASES), "--manifest", str(MANIFEST), "--corpus", str(corpus), "--output", str(output)],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout
    return json.loads(output.read_text(encoding="utf-8"))


def test_proposal_covers_all_cases_without_approval(tmp_path: Path) -> None:
    proposal = _run(tmp_path / "alignment.json")
    assert proposal["status"] == "PENDING_REVIEW"
    assert proposal["case_count"] == 60
    assert proposal["source_document_count"] == 37
    assert proposal["mapped_case_count"] == 60
    assert proposal["issues"] == ["evidence_chunks_unavailable"]
    assert proposal["real_service_acceptance"] is False
    assert len(proposal["case_to_source_documents"]) == 60


def test_proposal_is_deterministic_and_does_not_change_cases(tmp_path: Path) -> None:
    before = hashlib.sha256(CASES.read_bytes()).hexdigest()
    first = _run(tmp_path / "first.json")
    second = _run(tmp_path / "second.json")
    assert first == second
    assert hashlib.sha256(CASES.read_bytes()).hexdigest() == before
    assert all("query" not in check for check in first["case_checks"])


def test_proposal_requires_explicit_corpus_for_chunk_evidence(tmp_path: Path) -> None:
    proposal = _run(tmp_path / "alignment.json")
    assert "evidence_chunks_unavailable" in proposal["issues"]

    corpus = tmp_path / "documents.jsonl"
    corpus.write_text(json.dumps({
        "document_id": "aligned-product-bedding-v1",
        "chunks": [{"chunk_id": "chunk-1", "source_position": {"line_start": 1}}],
    }) + "\n", encoding="utf-8")
    proposal = _run_with_corpus(tmp_path / "alignment-with-corpus.json", corpus)
    check = proposal["case_checks"][0]["document_checks"][0]
    assert "evidence_chunks_unavailable" not in proposal["issues"]
    assert check["evidence_chunks"] == [{"chunk_id": "chunk-1", "source_position": {"line_start": 1}}]


def test_explicit_metadata_version_is_bound(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"case_id": "case-1", "expected_doc_ids": [], "authorization": {}}) + "\n", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({
        "case_count": 1,
        "eval_set_version": "revision-v1",
        "sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("documents: []\n", encoding="utf-8")
    proposal = revision.build_proposal(cases, manifest, metadata_path=metadata)
    assert proposal["eval_set_version"] == "revision-v1"
    assert proposal["alignment_version"].startswith("revision-v1-to-")
    assert proposal["metadata_sha256"] == hashlib.sha256(metadata.read_bytes()).hexdigest()
