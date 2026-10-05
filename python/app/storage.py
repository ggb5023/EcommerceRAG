"""Restricted object storage adapters for ingestion artifacts.

The storage boundary is deliberately independent from the M1 gRPC path.  The
filesystem adapter is used by offline tests; the Alibaba OSS adapter is only
constructed when its optional SDK and reviewed credentials are available.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

OSS_CONFIG_PATH = Path("/etc/ecommerce-rag/oss.env")


class ObjectStoreError(RuntimeError):
    """A redacted, operator-safe storage error."""


class ObjectStoreConfigError(ObjectStoreError):
    """Configuration or credential gate prevented storage use."""


@dataclass(frozen=True)
class OSSConfig:
    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    access_key_secret: str


def _parse_oss_file(path: str | Path = OSS_CONFIG_PATH) -> dict[str, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ObjectStoreConfigError("OSS configuration file is unavailable") from exc
    values: dict[str, str] = {}
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ObjectStoreConfigError(f"invalid OSS configuration line {line_no}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"OSS_[A-Z0-9_]+", key) or key in values:
            raise ObjectStoreConfigError(f"invalid OSS configuration key at line {line_no}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_oss_config(path: str | Path = OSS_CONFIG_PATH) -> OSSConfig:
    values = _parse_oss_file(path)
    required = {
        "endpoint": values.get("OSS_ENDPOINT", "").strip(),
        "bucket": values.get("OSS_BUCKET", "").strip(),
        "region": values.get("OSS_REGION", "").strip(),
        "access_key_id": values.get("OSS_ACCESS_KEY_ID", "").strip(),
        "access_key_secret": values.get("OSS_ACCESS_KEY_SECRET", "").strip(),
    }
    if not all(required.values()):
        raise ObjectStoreConfigError("OSS configuration is incomplete")
    if not re.fullmatch(r"https?://[^/]+/?", required["endpoint"]):
        raise ObjectStoreConfigError("OSS endpoint is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", required["bucket"]):
        raise ObjectStoreConfigError("OSS bucket is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", required["region"]):
        raise ObjectStoreConfigError("OSS region is invalid")
    return OSSConfig(**required)


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    sha256: str
    size_bytes: int
    content_type: str
    etag: str | None = None


class ObjectStore(Protocol):
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
    ) -> ObjectMetadata: ...

    def get_bytes(
        self, key: str, *, expected_sha256: str | None = None
    ) -> tuple[bytes, ObjectMetadata]: ...

    def delete(self, key: str) -> None: ...

    def head(self, key: str) -> ObjectMetadata: ...

    def presign_get(self, key: str, *, expires_s: int = 300) -> str: ...


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > 768:
        raise ObjectStoreError("object key is invalid")
    if "\\" in key or key.startswith("/"):
        raise ObjectStoreError("object key is invalid")
    parts = key.split("/")
    if len(parts) != 5 or any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ObjectStoreError("object key must have tenant/shop/version/artifact/hash")
    if parts[0] in {".", ".."} or parts[1] in {".", ".."}:
        raise ObjectStoreError("object key is invalid")
    if not _ARTIFACT.fullmatch(parts[3]):
        raise ObjectStoreError("object artifact type is invalid")
    if not _SHA256.fullmatch(parts[4]):
        raise ObjectStoreError("object key hash is invalid")
    return "/".join(parts)


def object_key(
    tenant_id: str,
    shop_id: str,
    document_version_id: str,
    artifact_type: str,
    sha256: str,
) -> str:
    """Build the immutable five-part key used by both adapters."""

    key = f"{tenant_id}/{shop_id}/{document_version_id}/{artifact_type}/{sha256}"
    return _validate_key(key)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_key_hash(key: str, data: bytes, expected: str | None) -> str:
    actual = _digest(data)
    key_hash = key.rsplit("/", 1)[-1]
    if actual != key_hash:
        raise ObjectStoreError("object content hash does not match key")
    if expected is not None and (not _SHA256.fullmatch(expected) or actual != expected):
        raise ObjectStoreError("object content hash does not match expected hash")
    return actual


def _safe_relative(root: Path, key: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / PurePosixPath(key)).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ObjectStoreError("object key escapes storage root")
    return candidate


class FilesystemObjectStore:
    """Atomic local adapter used for deterministic, offline storage tests."""

    def __init__(self, root: str | Path, *, signing_secret: bytes | None = None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.signing_secret = signing_secret or secrets.token_bytes(32)

    def _path(self, key: str) -> Path:
        return _safe_relative(self.root, _validate_key(key))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
    ) -> ObjectMetadata:
        key = _validate_key(key)
        if not isinstance(data, bytes):
            raise ObjectStoreError("object data must be bytes")
        digest = _verify_key_hash(key, data, expected_sha256)
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_bytes(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ObjectStoreError("object upload failed") from exc
        return ObjectMetadata(key, digest, len(data), content_type, digest)

    def get_bytes(
        self, key: str, *, expected_sha256: str | None = None
    ) -> tuple[bytes, ObjectMetadata]:
        key = _validate_key(key)
        path = self._path(key)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ObjectStoreError("object download failed") from exc
        digest = _verify_key_hash(key, data, expected_sha256)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return data, ObjectMetadata(key, digest, len(data), content_type, digest)

    def head(self, key: str) -> ObjectMetadata:
        data, metadata = self.get_bytes(key)
        _ = data
        return metadata

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ObjectStoreError("object deletion failed") from exc

    def presign_get(self, key: str, *, expires_s: int = 300) -> str:
        key = _validate_key(key)
        if not 1 <= expires_s <= 3600:
            raise ObjectStoreError("signature expiry is out of range")
        expires_at = int(time.time()) + expires_s
        message = f"GET\n{key}\n{expires_at}".encode()
        signature = hmac.new(self.signing_secret, message, hashlib.sha256).hexdigest()
        return f"local-object://{quote(key, safe='/')}?expires={expires_at}&sig={signature}"


class _OSSBucket(Protocol):
    def put_object(self, key: str, data: bytes, headers: dict[str, str] | None = None) -> Any: ...
    def get_object(self, key: str) -> Any: ...
    def head_object(self, key: str) -> Any: ...
    def delete_object(self, key: str) -> Any: ...
    def sign_url(self, method: str, key: str, expires: int, headers: dict[str, str] | None = None) -> str: ...


class AlibabaOSSObjectStore:
    """Alibaba OSS adapter with an injectable bucket for contract tests.

    The SDK import is lazy so offline environments do not need cloud packages;
    construction without a bucket fails closed when the SDK is unavailable.
    """

    def __init__(
        self,
        *,
        bucket: _OSSBucket | None = None,
        config: OSSConfig | None = None,
        config_path: str | Path = OSS_CONFIG_PATH,
        endpoint: str | None = None,
        bucket_name: str | None = None,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
    ) -> None:
        if bucket is not None:
            self.bucket = bucket
            self.endpoint = endpoint or "https://oss.invalid"
            self.bucket_name = bucket_name or "test"
            return
        if any(value is not None for value in (endpoint, bucket_name, access_key_id, access_key_secret)):
            if not all((endpoint, bucket_name, access_key_id, access_key_secret)):
                raise ObjectStoreConfigError("OSS configuration is incomplete")
            values = OSSConfig(endpoint, bucket_name, "unknown", access_key_id, access_key_secret)
        else:
            values = config or load_oss_config(config_path)
        try:
            import oss2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ObjectStoreConfigError("OSS SDK is unavailable") from exc
        try:
            auth = oss2.Auth(values.access_key_id, values.access_key_secret)
            self.bucket = oss2.Bucket(auth, values.endpoint, values.bucket)
        except Exception as exc:
            raise ObjectStoreConfigError("OSS client initialization failed") from exc
        self.endpoint = values.endpoint
        self.bucket_name = values.bucket

    @staticmethod
    def _metadata(key: str, data: bytes, *, content_type: str, etag: str | None = None) -> ObjectMetadata:
        digest = _verify_key_hash(key, data, None)
        return ObjectMetadata(key, digest, len(data), content_type, etag or digest)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
    ) -> ObjectMetadata:
        key = _validate_key(key)
        digest = _verify_key_hash(key, data, expected_sha256)
        try:
            result = self.bucket.put_object(
                key, data, headers={"Content-Type": content_type, "x-oss-meta-sha256": digest}
            )
        except Exception as exc:
            raise ObjectStoreError("object upload failed") from exc
        if getattr(result, "status", 200) >= 300:
            raise ObjectStoreError("object upload failed")
        return ObjectMetadata(key, digest, len(data), content_type, digest)

    def get_bytes(
        self, key: str, *, expected_sha256: str | None = None
    ) -> tuple[bytes, ObjectMetadata]:
        key = _validate_key(key)
        try:
            result = self.bucket.get_object(key)
            data = result.read()
            headers = getattr(result, "headers", {}) or {}
        except Exception as exc:
            raise ObjectStoreError("object download failed") from exc
        digest = _verify_key_hash(key, data, expected_sha256)
        return data, ObjectMetadata(
            key,
            digest,
            len(data),
            headers.get("Content-Type", "application/octet-stream"),
            headers.get("ETag", digest),
        )

    def head(self, key: str) -> ObjectMetadata:
        key = _validate_key(key)
        try:
            result = self.bucket.head_object(key)
            headers = getattr(result, "headers", {}) or {}
            size = int(headers.get("Content-Length", "0"))
            stored_hash = headers.get("x-oss-meta-sha256")
        except Exception as exc:
            raise ObjectStoreError("object metadata lookup failed") from exc
        if not stored_hash or not _SHA256.fullmatch(stored_hash):
            raise ObjectStoreError("object metadata has no valid sha256")
        if stored_hash != key.rsplit("/", 1)[-1]:
            raise ObjectStoreError("object metadata hash does not match key")
        return ObjectMetadata(
            key, stored_hash, size, headers.get("Content-Type", "application/octet-stream"), headers.get("ETag")
        )

    def delete(self, key: str) -> None:
        key = _validate_key(key)
        try:
            result = self.bucket.delete_object(key)
        except Exception as exc:
            raise ObjectStoreError("object deletion failed") from exc
        if getattr(result, "status", 204) >= 300:
            raise ObjectStoreError("object deletion failed")

    def presign_get(self, key: str, *, expires_s: int = 300) -> str:
        key = _validate_key(key)
        if not 1 <= expires_s <= 3600:
            raise ObjectStoreError("signature expiry is out of range")
        try:
            return str(self.bucket.sign_url("GET", key, expires_s))
        except Exception as exc:
            raise ObjectStoreError("object signing failed") from exc
