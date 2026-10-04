from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("validate_alignment.py")


def _write_inputs(tmp_path: Path, alignment: dict, *, cases_sha: str | None = None) -> tuple[Path, Path, Path, Path]:
    cases_path = tmp_path / "cases.jsonl"
    cases = [
        {
            "case_id": f"case-{index}",
            "expected_doc_ids": [f"label-doc-{index}"],
            "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"},
        }
        for index in range(1, 61)
    ]
    cases_path.write_text("\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8")
    actual_sha = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps({"case_count": 60, "sha256": cases_sha or actual_sha, "eval_set_version": "test-v1"}),
        encoding="utf-8",
    )
    corpus_path = tmp_path / "documents.jsonl"
    corpus_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "document_id": f"actual-doc-{index}",
                    "tenant_id": "tenant-a",
                    "shop_id": "shop-a",
                    "chunks": [],
                }
            )
            for index in range(1, 61)
        )
        + "\n",
        encoding="utf-8",
    )
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text(json.dumps(alignment), encoding="utf-8")
    return cases_path, metadata_path, corpus_path, alignment_path


def _run(tmp_path: Path, alignment: dict, **kwargs) -> dict:
    paths = _write_inputs(tmp_path, alignment, **kwargs)
    output = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cases",
            str(paths[0]),
            "--metadata",
            str(paths[1]),
            "--corpus",
            str(paths[2]),
            "--alignment",
            str(paths[3]),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.stdout
    return json.loads(output.read_text(encoding="utf-8"))


def test_pending_empty_mapping_is_diagnosed_without_approval(tmp_path: Path) -> None:
    report = _run(tmp_path, {"status": "PENDING_REVIEW", "case_to_source_documents": {}, "real_service_acceptance": False})
    assert report["status"] == "PENDING_REVIEW"
    assert report["mapped_case_count"] == 0
    assert "alignment_pending_review" in report["issues"]
    assert "alignment_mapping_empty" in report["issues"]
    assert report["real_service_acceptance"] is False


def test_approved_complete_mapping_passes_validation_only(tmp_path: Path) -> None:
    mapping = {f"case-{index}": {f"label-doc-{index}": f"actual-doc-{index}"} for index in range(1, 61)}
    report = _run(
        tmp_path,
        {
            "status": "APPROVED",
            "case_to_source_documents": mapping,
            "real_service_acceptance": False,
        },
    )
    assert report["status"] == "PASS"
    assert report["mapped_case_count"] == 60
    assert report["issues"] == []


def test_scope_mismatch_is_not_approved(tmp_path: Path) -> None:
    mapping = {f"case-{index}": {f"label-doc-{index}": f"actual-doc-{index}"} for index in range(1, 61)}
    cases, metadata, corpus, alignment = _write_inputs(
        tmp_path,
        {
            "status": "APPROVED",
            "case_to_source_documents": mapping,
            "real_service_acceptance": False,
        },
    )
    documents = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines()]
    documents[0]["tenant_id"] = "tenant-b"
    documents[0]["shop_id"] = "shop-b"
    corpus.write_text("\n".join(json.dumps(document) for document in documents) + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(cases), "--metadata", str(metadata), "--corpus", str(corpus), "--alignment", str(alignment), "--output", str(output)],
        check=False,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "PENDING_REVIEW"
    assert any("source_document_scope_mismatch" in issue for issue in report["issues"])


def test_case_hash_drift_is_fail(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        {"status": "PENDING_REVIEW", "case_to_source_documents": {}, "real_service_acceptance": False},
        cases_sha="0" * 64,
    )
    assert report["status"] == "FAIL"
    assert "cases_sha256_mismatch" in report["issues"]
