from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).with_name("provider-readiness.py")
SPEC = importlib.util.spec_from_file_location("provider_readiness", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


BASE = {
    "PROVIDER_PROFILE": "aliyun-bailian",
    "BAILIAN_ENDPOINT": "https://dashscope.example.test",
    "BAILIAN_REGION": "cn-beijing",
    "CONTROL_MODEL": "control-secret-id",
    "EMBEDDING_MODEL": "embedding-secret-id",
    "RERANK_MODEL": "rerank-secret-id",
    "GENERATION_MODEL": "generation-secret-id",
    "EMBEDDING_DIMENSIONS": "1024",
    "PROVIDER_QUOTA_RPM": "10",
    "PROVIDER_TIMEOUT_S": "5",
    "DASHSCOPE_API_KEY": "api-key-must-not-appear",
}


def write_config(directory: str, values: dict[str, str]) -> Path:
    path = Path(directory) / "providers.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    return path


class ProviderReadinessTests(unittest.TestCase):
    def test_complete_cloud_config_is_ready_for_independent_smoke_only(self):
        with tempfile.TemporaryDirectory() as directory:
            report, exit_code = MODULE.build_report(write_config(directory, BASE))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "READY_FOR_INDEPENDENT_SMOKE")
        self.assertEqual(report["online_smoke"], "NOT_RUN")
        self.assertEqual(report["endpoint_host"], "dashscope.example.test")
        self.assertTrue(all(row["configured"] for row in report["slots"].values()))
        serialized = json.dumps(report)
        self.assertNotIn("api-key-must-not-appear", serialized)
        self.assertNotIn("control-secret-id", serialized)

    def test_missing_file_is_config_blocked_without_network(self):
        report, exit_code = MODULE.build_report(Path("/tmp/provider-readiness-missing.env"))
        self.assertEqual(exit_code, 3)
        self.assertEqual(report["status"], "CONFIG_BLOCKED")
        self.assertEqual(report["online_smoke"], "NOT_RUN")
        self.assertFalse(report["secret_values_saved"])

    def test_mock_profile_is_explicitly_mock_only(self):
        values = dict(BASE)
        values["PROVIDER_PROFILE"] = "mock"
        values.pop("DASHSCOPE_API_KEY")
        values.pop("BAILIAN_ENDPOINT")
        values.pop("BAILIAN_REGION")
        with tempfile.TemporaryDirectory() as directory:
            report, exit_code = MODULE.build_report(write_config(directory, values))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "MOCK_ONLY")
        self.assertFalse(report["real_service_acceptance"])


if __name__ == "__main__":
    unittest.main()
