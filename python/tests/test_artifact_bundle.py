from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.ingest.artifacts import (
    ArtifactBundleError,
    ArtifactInput,
    build_ingestion_artifacts,
    read_artifact_bundle,
    read_manifest,
    store_artifact_bundle,
)
from app.ingest.pipeline import MarkdownParser, chunk_elements_v2
from app.storage import FilesystemObjectStore, ObjectStoreError


class FailingStore:
    def __init__(self, delegate: FilesystemObjectStore, fail_on_put: int):
        self.delegate = delegate
        self.fail_on_put = fail_on_put
        self.put_count = 0

    def put_bytes(self, key, data, *, content_type, expected_sha256):
        self.put_count += 1
        if self.put_count == self.fail_on_put:
            raise ObjectStoreError("simulated storage failure")
        return self.delegate.put_bytes(
            key, data, content_type=content_type, expected_sha256=expected_sha256
        )

    def get_bytes(self, key, *, expected_sha256=None):
        return self.delegate.get_bytes(key, expected_sha256=expected_sha256)

    def head(self, key):
        return self.delegate.head(key)

    def delete(self, key):
        return self.delegate.delete(key)

    def presign_get(self, key, *, expires_s=300):
        return self.delegate.presign_get(key, expires_s=expires_s)


class ArtifactBundleTests(unittest.TestCase):
    def test_structured_builder_rejects_empty_parse_outputs(self):
        with self.assertRaisesRegex(ArtifactBundleError, "element"):
            build_ingestion_artifacts(
                raw=b"source",
                elements=(),
                chunks=(),
                parse_report={"status": "FAILED"},
            )

    def test_structured_parser_output_has_one_deterministic_bundle_shape(self):
        raw = b"# Shipping\n\nDelivery takes two days.\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.md"
            source.write_bytes(raw)
            metadata = {
                "tenant_id": "tenant-a",
                "shop_id": "shop-a",
                "title": "Shipping",
                "disclosure_class": "external_allowed",
                "effective_from": "2026-01-01",
            }
            elements = MarkdownParser().parse(
                source, document_id="doc-a", version_id="version-1", metadata=metadata
            )
            chunks = chunk_elements_v2(elements, max_chars=80)
            first = build_ingestion_artifacts(
                raw=raw,
                elements=elements,
                chunks=chunks,
                parse_report={"status": "PASS"},
            )
            second = build_ingestion_artifacts(
                raw=raw,
                elements=elements,
                chunks=chunks,
                parse_report={"status": "PASS"},
            )
            self.assertEqual(
                [(item.artifact_type, item.data) for item in first],
                [(item.artifact_type, item.data) for item in second],
            )
            parsed_rows = [json.loads(line) for line in first[1].data.splitlines()]
            chunk_rows = [json.loads(line) for line in first[2].data.splitlines()]
            self.assertTrue(parsed_rows)
            self.assertTrue(chunk_rows)
            self.assertIn("source_position", parsed_rows[0])
            self.assertIn("chunk_hash", chunk_rows[0])
            store = FilesystemObjectStore(directory + "/objects")
            bundle = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                artifacts=first,
            )
            read_manifest(store, bundle)
            artifacts = read_artifact_bundle(store, bundle)
            self.assertEqual(artifacts["raw"], raw)
            report_record = next(record for record in bundle.records if record.artifact_type == "parse-report")
            report_data, _ = store.get_bytes(report_record.object_key, expected_sha256=report_record.sha256)
            self.assertEqual(json.loads(report_data)["chunk_count"], len(chunks))

    def test_bundle_is_content_addressed_and_manifest_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory, signing_secret=b"s" * 32)
            bundle = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                parser_version="parser-v1",
                chunk_rule_version="structured-v2",
                artifacts=(
                    ArtifactInput("chunks", b"chunk metadata\n", "application/jsonl"),
                    ArtifactInput("raw", b"raw document", "text/plain"),
                    ArtifactInput("parsed", b"parsed document", "text/markdown"),
                ),
            )

            self.assertEqual(
                [record.artifact_type for record in bundle.records],
                ["chunks", "parsed", "raw"],
            )
            self.assertFalse(bundle.real_service_acceptance)
            self.assertEqual(read_manifest(store, bundle)["parser_version"], "parser-v1")
            manifest_data, _ = store.get_bytes(
                bundle.manifest.object_key, expected_sha256=bundle.manifest.sha256
            )
            self.assertNotIn(b"raw document", manifest_data)
            for record in bundle.all_records:
                self.assertEqual(record.object_key.split("/")[-1], record.sha256)
                self.assertEqual(store.head(record.object_key).sha256, record.sha256)

    def test_repeating_same_bundle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            inputs = (
                ArtifactInput("raw", b"raw", "text/plain"),
                ArtifactInput("parsed", b"parsed", "text/plain"),
            )
            first = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                parser_version="parser-v1",
                chunk_rule_version="structured-v2",
                artifacts=inputs,
            )
            second = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                parser_version="parser-v1",
                chunk_rule_version="structured-v2",
                artifacts=inputs,
            )
            self.assertEqual(second, first)
            self.assertEqual(read_artifact_bundle(store, second), {
                "parsed": b"parsed",
                "raw": b"raw",
            })
            files = [path for path in Path(directory).rglob("*") if path.is_file()]
            self.assertEqual(len(files), len(first.all_records))

    def test_failed_bundle_rolls_back_objects_written_by_this_call(self):
        with tempfile.TemporaryDirectory() as directory:
            filesystem = FilesystemObjectStore(directory)
            store = FailingStore(filesystem, fail_on_put=2)
            with self.assertRaises(ArtifactBundleError):
                store_artifact_bundle(
                    store,
                    tenant_id="tenant-a",
                    shop_id="shop-a",
                    document_version_id="version-1",
                    artifacts=(
                        ArtifactInput("raw", b"raw"),
                        ArtifactInput("parsed", b"parsed"),
                    ),
                )
            self.assertEqual(
                [path for path in Path(directory).rglob("*") if path.is_file()], []
            )

    def test_rejects_duplicate_or_reserved_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            common = {
                "tenant_id": "tenant-a",
                "shop_id": "shop-a",
                "document_version_id": "version-1",
            }
            with self.assertRaisesRegex(ArtifactBundleError, "unique"):
                store_artifact_bundle(
                    store,
                    **common,
                    artifacts=(ArtifactInput("raw", b"a"), ArtifactInput("raw", b"b")),
                )
            with self.assertRaisesRegex(ArtifactBundleError, "reserved"):
                store_artifact_bundle(
                    store,
                    **common,
                    artifacts=(ArtifactInput("artifact-manifest", b"a"),),
                )
            with self.assertRaises(ArtifactBundleError):
                store_artifact_bundle(
                    store,
                    **{**common, "tenant_id": "../tenant"},
                    artifacts=(ArtifactInput("raw", b"a"),),
                )

    def test_tampered_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            bundle = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                artifacts=(ArtifactInput("report", b"report", "application/json"),),
            )
            manifest_path = Path(directory) / bundle.manifest.object_key
            manifest_path.write_bytes(b"tampered")
            with self.assertRaises(ArtifactBundleError):
                read_manifest(store, bundle)

    def test_tampered_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            bundle = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                artifacts=(ArtifactInput("raw", b"raw"),),
            )
            artifact = next(record for record in bundle.records if record.artifact_type == "raw")
            (Path(directory) / artifact.object_key).write_bytes(b"tampered")
            with self.assertRaises(ArtifactBundleError):
                read_artifact_bundle(store, bundle)

    def test_missing_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            bundle = store_artifact_bundle(
                store,
                tenant_id="tenant-a",
                shop_id="shop-a",
                document_version_id="version-1",
                artifacts=(ArtifactInput("raw", b"raw"),),
            )
            artifact = next(record for record in bundle.records if record.artifact_type == "raw")
            (Path(directory) / artifact.object_key).unlink()
            with self.assertRaises(ArtifactBundleError):
                read_artifact_bundle(store, bundle)


if __name__ == "__main__":
    unittest.main()
