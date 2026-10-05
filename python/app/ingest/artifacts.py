"""Content-addressed persistence for parsed ingestion artifacts.

The bundle layer keeps object storage separate from the M1 request path.  It
stores raw input, parser output, chunks, and reports through the shared
ObjectStore contract, then writes a metadata-only manifest.  A failed bundle
write rolls back objects written by that invocation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.storage import ObjectMetadata, ObjectStore, ObjectStoreError, object_key

_ARTIFACT_MANIFEST_TYPE = "artifact-manifest"
_ARTIFACT_TYPE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class ArtifactBundleError(ObjectStoreError):
    """A bundle could not be persisted or verified."""


@dataclass(frozen=True)
class ArtifactInput:
    """One immutable artifact to store for a document version."""

    artifact_type: str
    data: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_type: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "object_key": self.object_key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class ArtifactBundle:
    tenant_id: str
    shop_id: str
    document_version_id: str
    records: tuple[ArtifactRecord, ...]
    manifest: ArtifactRecord
    artifact_set_sha256: str
    real_service_acceptance: bool = False

    @property
    def all_records(self) -> tuple[ArtifactRecord, ...]:
        return (*self.records, self.manifest)

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "shop_id": self.shop_id,
            "document_version_id": self.document_version_id,
            "artifacts": [record.as_dict() for record in self.records],
            "manifest": self.manifest.as_dict(),
            "artifact_set_sha256": self.artifact_set_sha256,
            "real_service_acceptance": self.real_service_acceptance,
        }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_input(item: ArtifactInput) -> None:
    if not isinstance(item.artifact_type, str) or not _ARTIFACT_TYPE.fullmatch(
        item.artifact_type
    ):
        raise ArtifactBundleError("artifact type is invalid")
    if item.artifact_type == _ARTIFACT_MANIFEST_TYPE:
        raise ArtifactBundleError("artifact manifest is reserved")
    if not isinstance(item.data, bytes):
        raise ArtifactBundleError("artifact data must be bytes")
    if not item.data:
        raise ArtifactBundleError("artifact data must not be empty")
    if not isinstance(item.content_type, str) or not item.content_type.strip():
        raise ArtifactBundleError("artifact content type is invalid")


def _record_from_metadata(
    item: ArtifactInput, key: str, expected_sha256: str, metadata: ObjectMetadata
) -> ArtifactRecord:
    if metadata.key != key or metadata.sha256 != expected_sha256:
        raise ArtifactBundleError("stored artifact metadata does not match content")
    if metadata.size_bytes != len(item.data):
        raise ArtifactBundleError("stored artifact size does not match content")
    return ArtifactRecord(
        artifact_type=item.artifact_type,
        object_key=key,
        sha256=expected_sha256,
        size_bytes=len(item.data),
        content_type=item.content_type,
    )


def _rollback(store: ObjectStore, keys: Iterable[str]) -> bool:
    clean = True
    for key in reversed(tuple(keys)):
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001 - vendor details must not escape
            clean = False
    return clean


def store_artifact_bundle(
    store: ObjectStore,
    *,
    tenant_id: str,
    shop_id: str,
    document_version_id: str,
    artifacts: Iterable[ArtifactInput],
    parser_version: str = "",
    chunk_rule_version: str = "",
) -> ArtifactBundle:
    """Persist a document version's artifacts and a metadata-only manifest.

    The manifest contains hashes, object keys, sizes, and versions only.  It
    never copies artifact contents.  All objects use content-addressed keys;
    a failed write attempts to delete every object written in this call.
    """

    items = tuple(artifacts)
    if not items:
        raise ArtifactBundleError("artifact bundle must contain an artifact")
    if not all(isinstance(item, ArtifactInput) for item in items):
        raise ArtifactBundleError("artifact bundle contains an invalid item")
    for item in items:
        _validate_input(item)
    artifact_types = [item.artifact_type for item in items]
    if len(set(artifact_types)) != len(artifact_types):
        raise ArtifactBundleError("artifact types must be unique")

    # object_key performs the same segment and hash validation as both storage
    # adapters, so identifiers cannot introduce path traversal or separators.
    records: list[ArtifactRecord] = []
    written_keys: list[str] = []
    try:
        for item in sorted(items, key=lambda value: value.artifact_type):
            digest = _sha256(item.data)
            key = object_key(
                tenant_id, shop_id, document_version_id, item.artifact_type, digest
            )
            metadata = store.put_bytes(
                key,
                item.data,
                content_type=item.content_type,
                expected_sha256=digest,
            )
            records.append(_record_from_metadata(item, key, digest, metadata))
            written_keys.append(key)

        record_payload = [record.as_dict() for record in records]
        artifact_set_sha256 = _sha256(_canonical({"artifacts": record_payload}))
        manifest_body = {
            "manifest_version": "artifact-manifest-v1",
            "tenant_id": tenant_id,
            "shop_id": shop_id,
            "document_version_id": document_version_id,
            "parser_version": parser_version,
            "chunk_rule_version": chunk_rule_version,
            "artifacts": record_payload,
            "artifact_set_sha256": artifact_set_sha256,
            "real_service_acceptance": False,
        }
        manifest_data = _canonical(manifest_body)
        manifest_digest = _sha256(manifest_data)
        manifest_key = object_key(
            tenant_id,
            shop_id,
            document_version_id,
            _ARTIFACT_MANIFEST_TYPE,
            manifest_digest,
        )
        manifest_metadata = store.put_bytes(
            manifest_key,
            manifest_data,
            content_type="application/json",
            expected_sha256=manifest_digest,
        )
        manifest_item = ArtifactInput(_ARTIFACT_MANIFEST_TYPE, manifest_data, "application/json")
        manifest_record = _record_from_metadata(
            manifest_item, manifest_key, manifest_digest, manifest_metadata
        )
        written_keys.append(manifest_key)
        return ArtifactBundle(
            tenant_id=tenant_id,
            shop_id=shop_id,
            document_version_id=document_version_id,
            records=tuple(records),
            manifest=manifest_record,
            artifact_set_sha256=artifact_set_sha256,
        )
    except ArtifactBundleError:
        clean = _rollback(store, written_keys)
        if not clean:
            raise ArtifactBundleError("artifact bundle failed and cleanup was incomplete")
        raise
    except Exception as exc:
        clean = _rollback(store, written_keys)
        if not clean:
            raise ArtifactBundleError("artifact bundle failed and cleanup was incomplete") from exc
        raise ArtifactBundleError("artifact bundle write failed") from exc


def read_manifest(store: ObjectStore, bundle: ArtifactBundle) -> dict[str, object]:
    """Read and verify a previously stored bundle manifest."""

    try:
        data, metadata = store.get_bytes(
            bundle.manifest.object_key, expected_sha256=bundle.manifest.sha256
        )
    except ObjectStoreError as exc:
        raise ArtifactBundleError("artifact manifest could not be read") from exc
    if metadata.size_bytes != bundle.manifest.size_bytes:
        raise ArtifactBundleError("artifact manifest size changed")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactBundleError("artifact manifest is invalid") from exc
    if not isinstance(value, dict) or value.get("real_service_acceptance") is not False:
        raise ArtifactBundleError("artifact manifest boundary is invalid")
    expected = value.get("artifact_set_sha256")
    actual = _sha256(_canonical({"artifacts": value.get("artifacts")}))
    if expected != actual or expected != bundle.artifact_set_sha256:
        raise ArtifactBundleError("artifact manifest hash is invalid")
    return value
