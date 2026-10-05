from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

import grpc
from app.ingest.service import IngestService
from app.ingest.artifacts import read_manifest
from app.storage import FilesystemObjectStore
from rag.v1 import rag_pb2


class AbortContext:
    def abort(self, code, details):
        raise RuntimeError((code, details))


class IngestServiceTests(unittest.TestCase):
    def make_request(self, *, path: str = "docs/guide.md", content: bytes | None = None):
        raw = content or b"# Shipping\n\nSynthetic delivery takes two days.\n"
        digest = hashlib.sha256(raw).hexdigest()
        manifest = (
            "schema_version: 1\n"
            "pipeline_version: synthetic-parser-v1\n"
            "source:\n  id: synthetic-source\n  name: Synthetic source\n"
            "documents:\n"
            f"  - document_id: shipping\n    title: Shipping\n    path: {path}\n"
            "    format: markdown\n    disclosure_class: external_allowed\n"
            f"    sha256: {digest}\n"
            "    tenant_id: forged-tenant\n    shop_id: forged-shop\n"
        )
        return rag_pb2.ParsePackageRequest(
            manifest_yaml=manifest,
            files=[rag_pb2.PackageFile(path=path, content=raw)],
            tenant_id="server-tenant",
            shop_id="server-shop",
        )

    def test_parses_manifest_files_and_uses_server_scope(self):
        result = IngestService().ParsePackage(self.make_request(), AbortContext())
        self.assertEqual(result.source_id, "synthetic-source")
        self.assertEqual(result.documents[0].source_hash,
                         hashlib.sha256(b"# Shipping\n\nSynthetic delivery takes two days.\n").hexdigest())
        self.assertFalse(result.documents[0].external_allowed)
        self.assertIn('"tenant_id": "server-tenant"', result.documents[0].chunks[0].metadata_json)
        self.assertIn('"shop_id": "server-shop"', result.documents[0].chunks[0].metadata_json)
        self.assertEqual(result.documents[0].chunks[0].content_type, "heading")
        self.assertEqual(result.documents[0].chunks[0].char_start, 0)
        self.assertGreater(result.documents[0].chunks[0].char_end,
                           result.documents[0].chunks[0].char_start)

    def test_validate_package_returns_build_metadata_without_parsing(self):
        request = self.make_request()
        with patch("app.ingest.service.parser_for", side_effect=AssertionError("validator must not parse")):
            result = IngestService().ValidatePackage(request, AbortContext())
        self.assertEqual(result.source_id, "synthetic-source")
        self.assertEqual(result.pipeline_version, "synthetic-parser-v1")
        self.assertEqual(result.documents[0].document_id, "shipping")
        self.assertEqual(result.documents[0].disclosure_class, "external_allowed")
        self.assertRegex(result.documents[0].version_id, r"^v-[0-9a-f]{20}$")

    def test_validate_and_parse_share_hash_and_document_contract(self):
        request = self.make_request()
        service = IngestService()
        validated = service.ValidatePackage(request, AbortContext())
        parsed = service.ParsePackage(request, AbortContext())
        self.assertEqual(validated.manifest_sha256, parsed.manifest_sha256)
        self.assertEqual(validated.documents[0].document_id, parsed.documents[0].document_id)
        self.assertEqual(validated.documents[0].source_hash, parsed.documents[0].source_hash)

    def test_rejects_path_traversal_and_hash_mismatch(self):
        context = AbortContext()
        with self.assertRaises(RuntimeError) as raised:
            IngestService().ParsePackage(self.make_request(path="../guide.md"), context)
        self.assertEqual(raised.exception.args[0][0], grpc.StatusCode.INVALID_ARGUMENT)

        with self.assertRaises(RuntimeError):
            IngestService().ValidatePackage(self.make_request(path="../guide.md"), context)

        request = self.make_request()
        request.manifest_yaml = request.manifest_yaml.replace("sha256: ", "sha256: 0")
        with self.assertRaises(RuntimeError):
            IngestService().ParsePackage(request, context)

    def test_rejects_unreferenced_files(self):
        request = self.make_request()
        request.files.add(path="extra.md", content=b"not declared")
        with self.assertRaisesRegex(RuntimeError, "unreferenced"):
            IngestService().ParsePackage(request, AbortContext())

    def test_rejects_documents_that_produce_no_chunks(self):
        request = self.make_request(content=b"\n\n")
        with self.assertRaisesRegex(RuntimeError, "no content chunks"):
            IngestService().ParsePackage(request, AbortContext())

    def test_synthetic_profile_returns_verified_artifact_bundle_reference(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {
                    "RAG_PROFILE": "synthetic_import_mock",
                    "INGEST_ARTIFACT_BUNDLE_ROOT": directory,
                },
                clear=False,
            ):
                result = IngestService().ParsePackage(self.make_request(), AbortContext())
            reference = result.documents[0].artifact_bundle
            self.assertTrue(reference.document_version_id)
            self.assertRegex(reference.manifest_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(reference.artifact_set_sha256, r"^[0-9a-f]{64}$")
            self.assertFalse(reference.real_service_acceptance)
            self.assertEqual(
                {record.artifact_type for record in reference.artifacts},
                {"raw", "parsed", "chunks", "parse-report"},
            )
            store = FilesystemObjectStore(directory)
            for record in reference.artifacts:
                data, metadata = store.get_bytes(record.object_key, expected_sha256=record.sha256)
                self.assertEqual(len(data), record.size_bytes)
                self.assertEqual(metadata.sha256, record.sha256)
            manifest_record = store.head(reference.manifest_object_key)
            self.assertEqual(manifest_record.sha256, reference.manifest_sha256)
            # The helper also verifies artifact_set_sha256 and the boundary flag.
            bundle_like = type("Bundle", (), {
                "manifest": type("Manifest", (), {
                    "object_key": reference.manifest_object_key,
                    "sha256": reference.manifest_sha256,
                    "size_bytes": manifest_record.size_bytes,
                })(),
                "artifact_set_sha256": reference.artifact_set_sha256,
            })()
            self.assertEqual(read_manifest(store, bundle_like)["real_service_acceptance"], False)

    def test_artifact_bundle_root_is_not_allowed_for_m1_profile(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"RAG_PROFILE": "m1_fixture_mock", "INGEST_ARTIFACT_BUNDLE_ROOT": directory},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    IngestService().ParsePackage(self.make_request(), AbortContext())


if __name__ == "__main__":
    unittest.main()
