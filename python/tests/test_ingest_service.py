from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

import grpc

from app.ingest.service import IngestService
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


if __name__ == "__main__":
    unittest.main()
