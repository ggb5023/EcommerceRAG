from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from build_aligned_fixture import build_documents

SCRIPT = Path(__file__).with_name("build_aligned_fixture.py")
ROOT = SCRIPT.parents[1]
SOURCE_MANIFEST = ROOT / "data/synthetic/ecommerce-demo-v1/manifest.yaml"


def _chunk(*, disclosure_class: str = "external_allowed", effective_to: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        document_id="document-1",
        version_id="version-1",
        tenant_id="tenant-a",
        shop_id="shop-a",
        disclosure_class=disclosure_class,
        effective_from="2026-01-01",
        effective_to=effective_to,
        chunk_id="chunk-1",
        content="source content",
        source_ref="local://document-1/chunk-1",
        chunk_hash="a" * 64,
        source_position={"line_start": 1, "line_end": 1},
        rule_version="structured-v2",
    )


def test_document_policy_metadata_is_explicit_and_matches_chunks() -> None:
    document = build_documents([_chunk()])["document-1"]

    assert document["disclosure_class"] == "external_allowed"
    assert document["effective_from"] == "2026-01-01"
    assert document["effective_to"] is None
    chunk = document["chunks"][0]
    assert {
        key: chunk[key]
        for key in ("disclosure_class", "effective_from", "effective_to")
    } == {
        key: document[key]
        for key in ("disclosure_class", "effective_from", "effective_to")
    }


def test_inconsistent_document_policy_metadata_fails() -> None:
    with pytest.raises(ValueError, match="inconsistent policy metadata for document-1"):
        build_documents([_chunk(), _chunk(effective_to="2026-10-01")])


def test_generated_corpus_keeps_policy_metadata_on_documents_and_chunks(tmp_path: Path) -> None:
    output = tmp_path / "aligned"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(SOURCE_MANIFEST),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["documents"] > 0
    for line in (output / "documents.jsonl").read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        for chunk in document["chunks"]:
            for key in ("disclosure_class", "effective_from", "effective_to"):
                assert chunk[key] == document[key]
