from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from app.ingest.bundles import BundleRunError, run_manifest_bundle
from app.storage import FilesystemObjectStore

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "data/synthetic/ecommerce-demo-v1/manifest.yaml"


def test_manifest_bundle_binds_all_content_documents_and_skips_acl(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    output = tmp_path / "reports" / "bundle.json"

    report = run_manifest_bundle(MANIFEST, store, output=output)

    assert report["summary"] == {
        "documents": 10,
        "bundled": 9,
        "skipped_control": 1,
        "failed": 0,
    }
    assert report["real_service_acceptance"] is False
    assert report["raw_content_saved"] is False
    assert output.is_file()
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == report
    assert all(
        set(item) >= {"document_id", "document_version_id", "source_hash", "status"}
        for item in report["documents"]
    )
    assert len(list((tmp_path / "objects").rglob("*"))) > 10


def test_bundle_failure_removes_objects_written_by_the_run(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")

    original = __import__("app.ingest.pipeline", fromlist=["parser_for"]).parser_for

    def parser_for_with_failure(format_name: str):
        parser = original(format_name)
        if format_name != "markdown":
            return parser

        class FailingParser:
            def parse(self, path, *, document_id, version_id, metadata):
                if document_id == "syn-policy-a":
                    raise ValueError("fixture parser failure")
                return parser.parse(
                    path,
                    document_id=document_id,
                    version_id=version_id,
                    metadata=metadata,
                )

        return FailingParser()

    with patch("app.ingest.bundles.parser_for", side_effect=parser_for_with_failure), pytest.raises(
        BundleRunError, match="syn-policy-a"
    ):
        run_manifest_bundle(MANIFEST, store)
    assert not [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
