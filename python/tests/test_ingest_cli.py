from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.ingest.cli import import_dataset, validate_manifest
from app.ingest.pipeline import LocalIndex, load_manifest_index

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
        self.assertEqual(len(result.files), 9)
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
        self.assertFalse(changed.ok)
        self.assertTrue(any("sha256: mismatch" in error for error in changed.errors))

    def test_rejects_declared_hash_mismatch(self):
        path = self.manifest()
        text = path.read_text()
        marker = "sha256: 806c6510079cdf8fb515caee449d334afd48e66ceed31fe4ba99f25ea7c233a8"
        path.write_text(text.replace(marker, "sha256: " + "0" * 64, 1))
        result = validate_manifest(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("sha256:" in error for error in result.errors))

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

    def test_markdown_csv_and_acl_pipeline_retrieval(self):
        index = load_manifest_index(self.manifest())
        results = index.search("保温杯 容量", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})
        self.assertTrue(results)
        self.assertEqual(results[0]["document_id"], "syn-products-a")
        self.assertTrue(results[0]["is_mock"])
        self.assertIn("chunk_id", results[0])
        other = index.search("万用表", tenant_id="demo-tenant-b", allowed_shop_ids={"demo-shop-central"})
        self.assertTrue(other)
        self.assertTrue(all("demo-tenant-a" not in item["content"] for item in other))

    def test_selected_shop_must_also_be_in_authorized_shop_scope(self):
        source = load_manifest_index(self.manifest()).chunks[0]
        west = replace(source, shop_id="demo-shop-west", chunk_id="west-only")
        index = LocalIndex([source, west])
        denied = index.search(source.content, tenant_id=source.tenant_id, shop_id="demo-shop-west",
                              allowed_shop_ids={"demo-shop-east"})
        self.assertEqual(denied, [])
        scoped = index.search(source.content, tenant_id=source.tenant_id,
                              allowed_shop_ids={"demo-shop-east"})
        self.assertEqual({item["shop_id"] for item in scoped}, {"demo-shop-east"})
        self.assertEqual(index.search(source.content, tenant_id=source.tenant_id,
                                      allowed_shop_ids=set()), [])

    def test_effective_date_and_disclosure_filter(self):
        index = load_manifest_index(self.manifest())
        self.assertEqual(index.search("库存", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"}, business_date="2026-10-01"), [])
        admin = index.search("库存", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"}, role="admin", business_date="2026-09-30")
        self.assertTrue(admin)
        self.assertTrue(all(item["disclosure_class"] == "internal_only" for item in admin))

    def test_version_switch_keeps_old_index_isolated(self):
        first = load_manifest_index(self.manifest(), version_id="version-old")
        second = load_manifest_index(self.manifest(), version_id="version-new")
        first_results = first.search("保温杯 容量", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})
        second_results = second.search("保温杯 容量", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})
        self.assertTrue(first_results and second_results)
        self.assertTrue(all(item["version_id"] == "version-old" for item in first_results))
        self.assertTrue(all(item["version_id"] == "version-new" for item in second_results))
        self.assertNotEqual(first_results[0]["chunk_id"], second_results[0]["chunk_id"])

    def test_active_version_prevents_old_version_results(self):
        old = load_manifest_index(self.manifest(), version_id="version-old")
        new = load_manifest_index(self.manifest(), version_id="version-new")
        old.add(new.chunks)
        result = old.search("配送 退货", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})
        self.assertTrue(result)
        self.assertTrue(all(item["version_id"] == "version-old" for item in result))
        old.activate("demo-tenant-a", "syn-policy-a", "version-new")
        result = old.search("配送 退货", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})
        self.assertTrue(any(item["document_id"] == "syn-policy-a" for item in result))
        self.assertTrue(all(item["version_id"] == "version-new" for item in result if item["document_id"] == "syn-policy-a"))

    def test_evidence_has_traceable_citation_fields(self):
        index = load_manifest_index(self.manifest())
        item = index.search("配送 退货", tenant_id="demo-tenant-a", allowed_shop_ids={"demo-shop-east"})[0]
        for field in ("document_id", "version_id", "chunk_id", "source_ref", "content", "score", "rank", "citation_index", "disclosure_class", "is_mock"):
            self.assertIn(field, item)

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
                self.assertFalse(ok)
                self.assertIn("validation failed", message)
                payload = json.loads(next(Path(state).glob("*.json")).read_text())
                self.assertEqual(payload["synthetic"], True)
                self.assertEqual(payload["license"], "internal-generated")
            finally:
                cli._state_root = original


if __name__ == "__main__":
    unittest.main()
