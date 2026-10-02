import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


VALIDATOR = load("validate_public_sources", ROOT / "eval/validate_public_sources.py")
sys.modules["validate_public_sources"] = VALIDATOR
BASELINE = load("run_public_data_baseline", ROOT / "eval/run_public_data_baseline.py")


class PublicDataSourceTests(unittest.TestCase):
    def setUp(self):
        self.manifest_path = ROOT / "eval/public-data-sources.manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def test_current_sources_are_pending_and_valid(self):
        self.assertEqual(VALIDATOR.validate_manifest(self.manifest), [])
        self.assertTrue(all(source["download_status"] == "not_downloaded" for source in self.manifest["sources"]))

    def test_pending_source_baseline_is_not_run_without_reading_input(self):
        report = BASELINE.report_for_source(self.manifest, "amazon-esci", None, None, ROOT)
        self.assertEqual(report["status"], "NOT_RUN")
        self.assertIsNone(report["input_sha256"])
        self.assertFalse(report["real_service_acceptance"])

    def test_manifest_rejects_non_null_hash_for_not_downloaded_source(self):
        changed = json.loads(json.dumps(self.manifest))
        changed["sources"][0]["sha256"] = "0" * 64
        self.assertTrue(any("null sha256" in error for error in VALIDATOR.validate_manifest(changed)))

    def test_manifest_rejects_overlap_between_allowed_and_forbidden_use(self):
        changed = json.loads(json.dumps(self.manifest))
        changed["sources"][0]["forbidden_use"].append("product ranking")
        self.assertTrue(any("overlaps" in error for error in VALIDATOR.validate_manifest(changed)))

    def test_manifest_rejects_scope_drift_for_a_fixed_source(self):
        changed = json.loads(json.dumps(self.manifest))
        changed["sources"][0]["allowed_use"] = ["merchant policy truth"]
        self.assertTrue(any("fixed source scope" in error for error in VALIDATOR.validate_manifest(changed)))

    def test_restricted_verified_input_computes_metrics(self):
        changed = json.loads(json.dumps(self.manifest))
        source = changed["sources"][0]
        source.update({
            "status": "verified_downloaded",
            "revision": "abc123",
            "version_or_revision": "abc123",
            "original_source_terms_status": "verified",
            "license_status": "verified_apache-2.0",
            "download_status": "downloaded",
            "download_date": "2026-10-02",
            "actual_filename": "esci-test.jsonl",
            "restricted_storage_location": "outside_git_restricted_directory",
        })
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "esci-test.jsonl"
            input_path.write_text(
                '{"query_id":"q1","product_id":"p2","relevance":0,"rank":1}\n'
                '{"query_id":"q1","product_id":"p1","relevance":1,"rank":2}\n'
                '{"query_id":"q1","product_id":"p1","relevance":1,"rank":2}\n',
                encoding="utf-8",
            )
            source["sha256"] = BASELINE.file_sha256(input_path)
            report = BASELINE.report_for_source(changed, "amazon-esci", input_path, None, ROOT)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["metrics"]["duplicate_records_removed"], 1)
        self.assertEqual(report["metrics"]["mrr"], 0.5)
        self.assertEqual(report["metrics"]["recall_at_5"], 1.0)

    def test_raw_input_inside_repository_is_rejected(self):
        changed = json.loads(json.dumps(self.manifest))
        source = changed["sources"][0]
        source.update({"download_status": "downloaded", "revision": "abc", "version_or_revision": "abc", "original_source_terms_status": "verified", "license_status": "verified", "download_date": "2026-10-02", "actual_filename": "fixture.jsonl"})
        input_path = ROOT / "eval" / "fixture.jsonl"
        input_path.write_text('{"product_id":"p1"}\n', encoding="utf-8")
        try:
            source["sha256"] = BASELINE.file_sha256(input_path)
            with self.assertRaises(ValueError):
                BASELINE.report_for_source(changed, "amazon-esci", input_path, None, ROOT)
        finally:
            input_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
