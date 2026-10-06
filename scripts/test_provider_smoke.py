import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).with_name("provider-smoke.py")
SPEC = importlib.util.spec_from_file_location("provider_smoke", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProviderSmokeFixtureTests(unittest.TestCase):
    class Result:
        def __init__(self, model="model", request_id="request", usage=object()):
            self.model = model
            self.request_id = request_id
            self.usage = usage

    def test_live_metadata_handles_embedding_result_sequence(self):
        metadata = MODULE._live_result_metadata(
            "embedding", [self.Result(), self.Result()]
        )
        self.assertTrue(metadata["metadata_complete"])
        self.assertTrue(metadata["request_id_present"])
        self.assertTrue(metadata["usage_present"])
        self.assertEqual(len(metadata["model_fingerprint"]), 12)
        self.assertEqual(len(metadata["request_id_fingerprint"]), 12)

    def test_live_metadata_rejects_missing_embedding_usage(self):
        metadata = MODULE._live_result_metadata(
            "embedding", [self.Result(usage=None)]
        )
        self.assertFalse(metadata["metadata_complete"])
        self.assertEqual(metadata["error_code"], "missing_usage")

    def test_repository_fixture_validates_without_provider_config(self):
        report = MODULE.validate_response_fixture(
            PATH.with_name("provider-smoke-fixture.json")
        )
        self.assertEqual(len(report["slots"]), 4)
        self.assertFalse(report["raw_payload_saved"])

    def test_rejects_bad_embedding_dimension(self):
        payload = json.loads(PATH.with_name("provider-smoke-fixture.json").read_text())
        payload["responses"][1]["dimensions"] = 768
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            fixture.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                MODULE.validate_response_fixture(fixture)

    def test_rejects_duplicate_rerank_index(self):
        payload = json.loads(PATH.with_name("provider-smoke-fixture.json").read_text())
        payload["responses"][2]["items"][1]["index"] = 0
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            fixture.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                MODULE.validate_response_fixture(fixture)

    def test_rejects_invalid_finish_reason(self):
        payload = json.loads(PATH.with_name("provider-smoke-fixture.json").read_text())
        payload["responses"][0]["finish_reason"] = "unknown"
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            fixture.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                MODULE.validate_response_fixture(fixture)

    def test_rejects_invalid_embedding_shape_metadata(self):
        payload = json.loads(PATH.with_name("provider-smoke-fixture.json").read_text())
        payload["responses"][1]["dense_values"] = 768
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            fixture.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                MODULE.validate_response_fixture(fixture)


if __name__ == "__main__":
    unittest.main()
