from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

PATH = Path(__file__).resolve().parents[1] / "scripts/oss-smoke.py"
SPEC = importlib.util.spec_from_file_location("oss_smoke", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def config_file(path: Path) -> Path:
    path.write_text(
        """OSS_ENDPOINT=https://oss-cn-beijing-internal.aliyuncs.com
OSS_BUCKET=ecommercerag-dev
OSS_REGION=cn-beijing
OSS_ACCESS_KEY_ID=secret-access-id-test
OSS_ACCESS_KEY_SECRET=secret-access-key-test
""",
        encoding="utf-8",
    )
    return path


class FakeStore:
    def __init__(self, *, config):
        self.config = config
        self.data: dict[str, bytes] = {}

    def put_bytes(self, key, data, *, content_type, expected_sha256):
        self.data[key] = data
        return type("Metadata", (), {"sha256": expected_sha256, "size_bytes": len(data)})()

    def head(self, key):
        if key not in self.data:
            raise MODULE.ObjectStoreError("object not found")
        data = self.data[key]
        return type(
            "Metadata",
            (),
            {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)},
        )()

    def get_bytes(self, key, *, expected_sha256):
        data = self.data[key]
        return data, self.head(key)

    def presign_get(self, key, *, expires_s):
        return f"fake://{key}?expires={expires_s}"

    def delete(self, key):
        self.data.pop(key, None)


class OSSSmokeTests(unittest.TestCase):
    def test_default_mode_never_fetches_and_redacts_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = config_file(Path(directory) / "oss.env")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = MODULE._local_gate(path)
            self.assertEqual(result, 3)
            self.assertTrue(
                "live flag is required" in output.getvalue()
                or "OSS SDK is unavailable" in output.getvalue()
            )
            self.assertNotIn("secret-access-id-test", output.getvalue())
            self.assertNotIn("secret-access-key-test", output.getvalue())

    def test_live_mode_rejects_non_dev_bucket_before_store_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = config_file(Path(directory) / "oss.env")
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "OSS_BUCKET=ecommercerag-dev", "OSS_BUCKET=production-bucket"
                ),
                encoding="utf-8",
            )
            created = False

            def make_store(*, config):
                nonlocal created
                created = True
                return FakeStore(config=config)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = MODULE._live(path, store_factory=make_store)
            self.assertEqual(result, 3)
            self.assertFalse(created)
            self.assertIn("BLOCKED", output.getvalue())

    def test_live_adapter_contract_passes_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = config_file(Path(directory) / "oss.env")
            store = None

            def make_store(*, config):
                nonlocal store
                store = FakeStore(config=config)
                return store

            def fetcher(url):
                if url.endswith("expires=1"):
                    raise HTTPError(url, 403, "expired", {}, None)
                key = url.removeprefix("fake://").split("?", 1)[0]
                return store.data[key]

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = MODULE._live(
                    path, store_factory=make_store, fetcher=fetcher, sleeper=lambda _: None
                )
            self.assertEqual(result, 0)
            self.assertEqual(store.data, {})
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(all(report["checks"].values()))
            self.assertFalse(report["real_service_acceptance"])

    def test_live_operation_failure_still_attempts_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = config_file(Path(directory) / "oss.env")

            class DownloadFailStore(FakeStore):
                def get_bytes(self, key, *, expected_sha256):
                    raise RuntimeError("sensitive vendor error")

            store = None

            def make_store(*, config):
                nonlocal store
                store = DownloadFailStore(config=config)
                return store

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = MODULE._live(
                    path,
                    store_factory=make_store,
                    fetcher=lambda _: b"unused",
                    sleeper=lambda _: None,
                )
            self.assertEqual(result, 4)
            self.assertEqual(store.data, {})
            self.assertNotIn("sensitive vendor error", output.getvalue())
            report = json.loads(output.getvalue())
            self.assertTrue(report["checks"]["delete"])
            self.assertFalse(report["checks"]["live_operation_error"])


if __name__ == "__main__":
    unittest.main()
