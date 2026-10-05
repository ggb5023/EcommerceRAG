from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.storage import (
    AlibabaOSSObjectStore,
    FilesystemObjectStore,
    ObjectStoreConfigError,
    ObjectStoreError,
    OSSConfig,
    load_oss_config,
    object_key,
)


class FakeResult:
    def __init__(self, data: bytes = b"", headers: dict[str, str] | None = None, status: int = 200):
        self.data = data
        self.headers = headers or {}
        self.status = status

    def read(self) -> bytes:
        return self.data


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, key, data, headers=None):
        self.calls.append(("put", key))
        self.objects[key] = (data, dict(headers or {}))
        return FakeResult(status=200)

    def get_object(self, key):
        self.calls.append(("get", key))
        data, headers = self.objects[key]
        return FakeResult(data, headers)

    def head_object(self, key):
        self.calls.append(("head", key))
        data, headers = self.objects[key]
        return FakeResult(headers={**headers, "Content-Length": str(len(data)), "ETag": "etag"})

    def delete_object(self, key):
        self.calls.append(("delete", key))
        self.objects.pop(key, None)
        return FakeResult(status=204)

    def sign_url(self, method, key, expires, headers=None):
        self.calls.append(("sign", key))
        return f"https://oss.test/{key}?method={method}&expires={expires}"


def key_for(data: bytes, artifact: str = "raw") -> str:
    return object_key("tenant-a", "shop-a", "version-1", artifact, hashlib.sha256(data).hexdigest())


class ObjectKeyTests(unittest.TestCase):
    def test_key_requires_hash_and_rejects_traversal(self):
        with self.assertRaises(ObjectStoreError):
            object_key("tenant-a", "shop-a", "version-1", "raw", "bad")
        with self.assertRaises(ObjectStoreError):
            FilesystemObjectStore(tempfile.mkdtemp()).head("../shop/version/raw/" + "0" * 64)


class FilesystemObjectStoreTests(unittest.TestCase):
    def test_put_get_hash_delete_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory, signing_secret=b"s" * 32)
            data = b"artifact"
            key = key_for(data)
            written = store.put_bytes(key, data, content_type="text/plain", expected_sha256=key.rsplit("/", 1)[-1])
            self.assertEqual(written.size_bytes, len(data))
            self.assertEqual(store.get_bytes(key)[0], data)
            self.assertIn("expires=", store.presign_get(key))
            self.assertEqual(store.head(key).sha256, written.sha256)
            store.delete(key)
            with self.assertRaises(ObjectStoreError):
                store.get_bytes(key)

    def test_tamper_and_wrong_key_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(directory)
            data = b"artifact"
            key = key_for(data)
            store.put_bytes(key, data)
            path = Path(directory) / key
            path.write_bytes(b"tampered")
            with self.assertRaises(ObjectStoreError):
                store.get_bytes(key)
            with self.assertRaises(ObjectStoreError):
                store.put_bytes(key, b"other")


class AlibabaOSSObjectStoreTests(unittest.TestCase):
    def test_fake_bucket_round_trip_and_metadata(self):
        bucket = FakeBucket()
        store = AlibabaOSSObjectStore(bucket=bucket)
        data = b"oss artifact"
        key = key_for(data, "parsed")
        store.put_bytes(key, data, content_type="application/json")
        loaded, metadata = store.get_bytes(key, expected_sha256=key.rsplit("/", 1)[-1])
        self.assertEqual(loaded, data)
        self.assertEqual(metadata.sha256, key.rsplit("/", 1)[-1])
        self.assertEqual(store.head(key).size_bytes, len(data))
        self.assertIn("expires=60", store.presign_get(key, expires_s=60))
        store.delete(key)
        self.assertEqual(bucket.objects, {})

    def test_sdk_missing_or_incomplete_config_is_blocked(self):
        with self.assertRaises(ObjectStoreConfigError):
            AlibabaOSSObjectStore(endpoint="https://oss.test", bucket_name="dev")

    def test_config_is_file_only_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oss.env"
            path.write_text(
                "OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com\n"
                "OSS_BUCKET=ecommercerag-dev\n"
                "OSS_REGION=cn-beijing\n"
                "OSS_ACCESS_KEY_ID=redacted-id\n"
                "OSS_ACCESS_KEY_SECRET=redacted-secret\n",
                encoding="utf-8",
            )
            config = load_oss_config(path)
        self.assertEqual(config.region, "cn-beijing")
        self.assertIsInstance(config, OSSConfig)

    def test_config_file_missing_is_not_replaced_by_process_environment(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ObjectStoreConfigError):
            AlibabaOSSObjectStore(config_path=Path(directory) / "missing.env")


if __name__ == "__main__":
    unittest.main()
