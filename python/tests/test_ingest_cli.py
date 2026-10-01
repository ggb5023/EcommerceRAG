from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.ingest.cli import import_dataset, validate_manifest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "data/synthetic/ecommerce-demo-v1"


class IngestValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dataset"
        shutil.copytree(SOURCE, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self):
        return self.root / "manifest.yaml"

    def test_clean_manifest_passes(self):
        result = validate_manifest(self.manifest())
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.files), 7)
        self.assertTrue(result.manifest_sha256)
        self.assertTrue(result.dataset_sha256)

    def test_rejects_path_traversal(self):
        path = self.manifest()
        text = path.read_text()
        path.write_text(text.replace("path: products.csv", "path: ../products.csv", 1))
        result = validate_manifest(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("safe relative" in error for error in result.errors))

    def test_rejects_duplicate_document_id(self):
        path = self.manifest()
        text = path.read_text()
        first = "document_id: syn-products-a"
        path.write_text(text.replace("document_id: syn-products-b", first, 1))
        result = validate_manifest(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("duplicate document_id" in error for error in result.errors))

    def test_rejects_cross_tenant_principal_shop(self):
        path = self.manifest()
        text = path.read_text()
        path.write_text(text.replace("shop_ids: [demo-shop-central]", "shop_ids: [demo-shop-east]", 1))
        result = validate_manifest(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("outside tenant" in error for error in result.errors))

    def test_rejects_hash_change_on_import_source(self):
        result = validate_manifest(self.manifest())
        self.assertTrue(result.ok, result.errors)
        (self.root / "faq.md").write_text((self.root / "faq.md").read_text() + "\nchanged\n")
        changed = validate_manifest(self.manifest())
        self.assertTrue(changed.ok, changed.errors)
        self.assertNotEqual(result.dataset_sha256, changed.dataset_sha256)

    def test_invalid_format_rejects_whole_package(self):
        path = self.manifest()
        text = path.read_text()
        path.write_text(text.replace("format: csv", "format: docx", 1))
        result = validate_manifest(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("unsupported format" in error for error in result.errors))

    def test_manifest_change_changes_manifest_hash_and_import_is_not_idempotent(self):
        result = validate_manifest(self.manifest())
        self.assertTrue(result.ok, result.errors)
        path = self.manifest()
        path.write_text(path.read_text() + "\n# manifest revision\n")
        changed = validate_manifest(path)
        self.assertTrue(changed.ok, changed.errors)
        self.assertNotEqual(result.manifest_sha256, changed.manifest_sha256)

    def test_acl_yaml_is_validated_and_imported_as_a_record(self):
        result = validate_manifest(self.manifest())
        self.assertTrue(result.ok, result.errors)
        with tempfile.TemporaryDirectory() as state:
            from app.ingest import cli
            original = cli._state_root
            cli._state_root = lambda: Path(state)
            try:
                ok, _ = import_dataset(result)
                self.assertTrue(ok)
                payload = json.loads((Path(state) / "ecommerce-demo-v1.json").read_text())
                self.assertTrue(any(record["document_id"] == "syn-acl-a" for record in payload["records"]))
            finally:
                cli._state_root = original

    def test_import_is_idempotent_then_updates_on_source_change(self):
        result = validate_manifest(self.manifest())
        self.assertTrue(result.ok, result.errors)
        with tempfile.TemporaryDirectory() as state:
            from app.ingest import cli
            original = cli._state_root
            cli._state_root = lambda: Path(state)
            try:
                ok, message = import_dataset(result)
                self.assertTrue(ok)
                self.assertIn("IMPORTED", message)
                ok, message = import_dataset(result)
                self.assertTrue(ok)
                self.assertIn("IDEMPOTENT", message)
                (self.root / "policies.md").write_text((self.root / "policies.md").read_text() + "\n新增合成说明。\n")
                changed = validate_manifest(self.manifest())
                ok, message = import_dataset(changed)
                self.assertTrue(ok)
                self.assertIn("UPDATED", message)
                payload = json.loads(next(Path(state).glob("*.json")).read_text())
                self.assertEqual(payload["synthetic"], True)
                self.assertEqual(payload["license"], "internal-generated")
            finally:
                cli._state_root = original


if __name__ == "__main__":
    unittest.main()
