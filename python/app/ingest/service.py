"""Validate and parse bounded manifest packages for the Go gateway."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath

import grpc
import yaml

from app.ingest.artifacts import (
    ArtifactBundle,
    build_ingestion_artifacts,
    store_artifact_bundle,
)
from app.ingest.pipeline import chunk_elements_v2, parser_for
from app.storage import FilesystemObjectStore, ObjectStore
from rag.v1 import rag_pb2, rag_pb2_grpc

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENTS = 100
MAX_CHUNKS = 10_000
ALLOWED_FORMATS = {"markdown", "csv", "docx"}
ALLOWED_DISCLOSURE = {"external_allowed", "internal_only", "unclassified"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fail(context, message: str):
    context.abort(grpc.StatusCode.INVALID_ARGUMENT, message)


def _artifact_store() -> ObjectStore | None:
    """Return the opt-in synthetic bundle store; never enable it for M1."""

    configured = os.environ.get("INGEST_ARTIFACT_BUNDLE_ROOT", "").strip()
    if not configured:
        return None
    if os.environ.get("RAG_PROFILE") != "synthetic_import_mock":
        raise RuntimeError("artifact bundle storage requires synthetic_import_mock")
    root = Path(configured)
    if not root.is_absolute():
        raise RuntimeError("artifact bundle root must be absolute")
    return FilesystemObjectStore(root)


def _delete_bundles(store: ObjectStore | None, bundles: list[ArtifactBundle]) -> bool:
    """Delete all objects written during a failed parse run.

    Cleanup is part of the failure contract.  Callers must be able to
    distinguish a clean rollback from a rollback that left objects behind,
    while the underlying storage error remains redacted.
    """

    if store is None:
        return True
    clean = True
    for bundle in reversed(bundles):
        if not _delete_keys(store, [record.object_key for record in bundle.all_records]):
            clean = False
    return clean


def _delete_keys(store: ObjectStore, keys: list[str]) -> bool:
    clean = True
    for key in reversed(keys):
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001 - abort path must stay redacted
            clean = False
    return clean


def _artifact_cleanup_keys(request, context) -> list[str]:
    """Validate an internal bundle reference before allowing deletion."""

    tenant_id, shop_id = request.tenant_id, request.shop_id
    reference = request.artifact_bundle
    if not tenant_id or not shop_id or reference is None:
        _fail(context, "artifact cleanup reference is invalid")
    version_id = reference.document_version_id
    if not re.fullmatch(r"v-[0-9a-f]{20}", version_id):
        _fail(context, "artifact cleanup reference is invalid")

    manifest_parts = reference.manifest_object_key.split("/")
    if (
        len(manifest_parts) != 5
        or manifest_parts[:3] != [tenant_id, shop_id, version_id]
        or manifest_parts[3] != "artifact-manifest"
        or manifest_parts[4] != reference.manifest_sha256
        or not SHA256.fullmatch(reference.manifest_sha256)
    ):
        _fail(context, "artifact cleanup reference is invalid")

    required = {"chunks", "parse-report", "parsed", "raw"}
    records = []
    seen = set()
    for record in reference.artifacts:
        artifact_type = record.artifact_type
        parts = record.object_key.split("/")
        if (
            artifact_type not in required
            or artifact_type in seen
            or record.size_bytes <= 0
            or not record.content_type
            or not SHA256.fullmatch(record.sha256)
            or len(parts) != 5
            or parts[:3] != [tenant_id, shop_id, version_id]
            or parts[3] != artifact_type
            or parts[4] != record.sha256
        ):
            _fail(context, "artifact cleanup reference is invalid")
        seen.add(artifact_type)
        records.append(record)
    if seen != required:
        _fail(context, "artifact cleanup reference is invalid")

    canonical = [
        {
            "artifact_type": record.artifact_type,
            "content_type": record.content_type,
            "object_key": record.object_key,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
        }
        for record in sorted(records, key=lambda item: item.artifact_type)
    ]
    artifact_set = hashlib.sha256(
        json.dumps({"artifacts": canonical}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if artifact_set != reference.artifact_set_sha256 or reference.real_service_acceptance:
        _fail(context, "artifact cleanup reference is invalid")
    return [record.object_key for record in records] + [reference.manifest_object_key]


def _bundle_reference(response, bundle: ArtifactBundle) -> None:
    reference = response.artifact_bundle
    reference.document_version_id = bundle.document_version_id
    reference.manifest_object_key = bundle.manifest.object_key
    reference.manifest_sha256 = bundle.manifest.sha256
    reference.artifact_set_sha256 = bundle.artifact_set_sha256
    reference.real_service_acceptance = False
    for record in bundle.records:
        reference.artifacts.add(
            artifact_type=record.artifact_type,
            object_key=record.object_key,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            content_type=record.content_type,
        )


def _date_text(value, field: str, context) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        _fail(context, f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail(context, f"{field} must be an ISO date")
    return parsed.isoformat()


def _inspect_package(request, context):
    if not request.tenant_id or not request.shop_id:
        _fail(context, "server authorization scope is required")
    manifest_bytes = request.manifest_yaml.encode("utf-8")
    if not manifest_bytes or len(manifest_bytes) > MAX_MANIFEST_BYTES:
        _fail(context, "manifest size is outside the allowed limit")
    if not 1 <= len(request.files) <= MAX_DOCUMENTS:
        _fail(context, "package file count is outside the allowed limit")

    uploaded: dict[str, bytes] = {}
    total = 0
    for item in request.files:
        path = PurePosixPath(item.path)
        if (not item.path or "\\" in item.path or path.is_absolute()
                or path.as_posix() != item.path
                or any(part in {"", ".", ".."} for part in path.parts)):
            _fail(context, "package contains an unsafe file path")
        if item.path in uploaded:
            _fail(context, "package contains a duplicate file path")
        total += len(item.content)
        if total > MAX_PACKAGE_BYTES:
            _fail(context, "package size exceeds the allowed limit")
        uploaded[item.path] = bytes(item.content)

    try:
        manifest = yaml.safe_load(request.manifest_yaml)
    except yaml.YAMLError:
        _fail(context, "manifest YAML is invalid")
    if not isinstance(manifest, dict) or isinstance(manifest.get("schema_version"), bool) or manifest.get("schema_version") != 1:
        _fail(context, "manifest schema_version must be 1")
    source = manifest.get("source")
    if not isinstance(source, dict):
        _fail(context, "manifest source is required")
    source_id, source_name = source.get("id"), source.get("name")
    pipeline_version = manifest.get("pipeline_version")
    if not isinstance(source_id, str) or not IDENTIFIER.fullmatch(source_id):
        _fail(context, "manifest source.id is invalid")
    if not isinstance(source_name, str) or not source_name.strip() or len(source_name) > 200:
        _fail(context, "manifest source.name is invalid")
    if not isinstance(pipeline_version, str) or not IDENTIFIER.fullmatch(pipeline_version):
        _fail(context, "manifest pipeline_version is invalid")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS:
        _fail(context, "manifest documents count is outside the allowed limit")

    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    used_paths: set[str] = set()
    document_ids: set[str] = set()
    validated = []
    for index, spec in enumerate(documents):
        if not isinstance(spec, dict):
            _fail(context, f"documents[{index}] must be a mapping")
        document_id, title, relative, fmt = (
            spec.get("document_id"), spec.get("title"), spec.get("path"), spec.get("format")
        )
        if not isinstance(document_id, str) or not IDENTIFIER.fullmatch(document_id) or document_id in document_ids:
            _fail(context, f"documents[{index}].document_id is invalid or duplicated")
        document_ids.add(document_id)
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            _fail(context, f"documents[{index}].title is invalid")
        if not isinstance(relative, str) or not relative or "\\" in relative:
            _fail(context, f"documents[{index}].path is invalid")
        relative_path = PurePosixPath(relative)
        if (relative_path.is_absolute() or relative_path.as_posix() != relative
                or any(part in {"", ".", ".."} for part in relative_path.parts)):
            _fail(context, f"documents[{index}].path is unsafe")
        if relative not in uploaded or relative in used_paths:
            _fail(context, f"documents[{index}].path is missing or duplicated")
        used_paths.add(relative)
        if not isinstance(fmt, str) or fmt not in ALLOWED_FORMATS:
            _fail(context, f"documents[{index}].format is unsupported")
        extension_ok = {
            "markdown": relative_path.suffix.lower() in {".md", ".markdown"},
            "csv": relative_path.suffix.lower() == ".csv",
            "docx": relative_path.suffix.lower() == ".docx",
        }[fmt]
        if not extension_ok:
            _fail(context, f"documents[{index}] format does not match its file")
        disclosure = spec.get("disclosure_class", "unclassified")
        if not isinstance(disclosure, str) or disclosure not in ALLOWED_DISCLOSURE:
            _fail(context, f"documents[{index}].disclosure_class is invalid")
        effective_from = _date_text(spec.get("effective_from"), "effective_from", context)
        effective_to = _date_text(spec.get("effective_to"), "effective_to", context)
        if effective_from and effective_to and effective_to <= effective_from:
            _fail(context, f"documents[{index}] effective_to must be after effective_from")

        content = uploaded[relative]
        source_hash = hashlib.sha256(content).hexdigest()
        declared_hash = spec.get("sha256")
        if not isinstance(declared_hash, str) or not SHA256.fullmatch(declared_hash) or declared_hash != source_hash:
            _fail(context, f"documents[{index}].sha256 does not match the uploaded file")
        version_id = "v-" + hashlib.sha256(
            f"{source_hash}\0{pipeline_version}\0{manifest_hash}".encode()
        ).hexdigest()[:20]
        validated.append({
            "document_id": document_id,
            "title": title.strip(),
            "format": fmt,
            "path": relative,
            "source_hash": source_hash,
            "disclosure_class": disclosure,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "version_id": version_id,
        })
    if used_paths != set(uploaded):
        _fail(context, "package contains unreferenced files")
    return {
        "source_id": source_id,
        "source_name": source_name.strip(),
        "pipeline_version": pipeline_version,
        "manifest_sha256": manifest_hash,
        "documents": validated,
        "uploaded": uploaded,
    }


class IngestService(rag_pb2_grpc.IngestServiceServicer):
    """Python validates and parses; Go owns authorization, persistence, and publication."""

    def ValidatePackage(self, request, context):
        package = _inspect_package(request, context)
        response = rag_pb2.ValidatePackageResponse(
            source_id=package["source_id"],
            source_name=package["source_name"],
            pipeline_version=package["pipeline_version"],
            manifest_sha256=package["manifest_sha256"],
        )
        for spec in package["documents"]:
            response.documents.add(**spec)
        return response

    def ParsePackage(self, request, context):
        package = _inspect_package(request, context)
        artifact_store: ObjectStore | None = None
        try:
            artifact_store = _artifact_store()
        except (OSError, RuntimeError, ValueError) as exc:
            _fail(context, f"artifact bundle configuration is invalid: {type(exc).__name__}")
        bundles: list[ArtifactBundle] = []

        def fail(message: str):
            if not _delete_bundles(artifact_store, bundles):
                _fail(context, "artifact bundle cleanup was incomplete")
            _fail(context, message)

        response = rag_pb2.ParsePackageResponse(
            source_id=package["source_id"],
            source_name=package["source_name"],
            pipeline_version=package["pipeline_version"],
            manifest_sha256=package["manifest_sha256"],
            manifest_yaml=request.manifest_yaml,
        )
        dataset_rows = []
        chunk_total = 0
        with tempfile.TemporaryDirectory(prefix="ecr-admin-parse-") as temporary:
            root = Path(temporary)
            for spec in package["documents"]:
                relative_path = PurePosixPath(spec["path"])
                target = root.joinpath(*relative_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(package["uploaded"][spec["path"]])
                metadata = {
                    "tenant_id": request.tenant_id,
                    "shop_id": request.shop_id,
                    "title": spec["title"],
                    "disclosure_class": spec["disclosure_class"],
                    "effective_from": spec["effective_from"] or None,
                    "effective_to": spec["effective_to"] or None,
                    "source_hash": spec["source_hash"],
                }
                try:
                    elements = parser_for(spec["format"]).parse(
                        target, document_id=spec["document_id"],
                        version_id=spec["version_id"], metadata=metadata,
                    )
                    chunks = chunk_elements_v2(elements)
                except (OSError, UnicodeError, ValueError, KeyError, RuntimeError):
                    fail("document could not be parsed")
                if not chunks:
                    fail("document produced no content chunks")
                parsed = response.documents.add(
                    document_id=spec["document_id"],
                    title=spec["title"],
                    format=spec["format"],
                    path=spec["path"],
                    source_hash=spec["source_hash"],
                    disclosure_class=spec["disclosure_class"],
                    external_allowed=False,
                    effective_from=spec["effective_from"],
                    effective_to=spec["effective_to"],
                )
                for chunk_index, chunk in enumerate(chunks):
                    chunk_total += 1
                    if chunk_total > MAX_CHUNKS:
                        _fail(context, "parsed package exceeds the chunk limit")
                    position = chunk.source_position
                    parsed.chunks.add(
                        chunk_index=chunk_index,
                        section_seq=chunk.section_seq,
                        section_chunk_index=chunk.chunk_index,
                        char_start=int(position.get("char_start", 0)),
                        char_end=int(position.get("char_end", len(chunk.content))),
                        title=chunk.title,
                        heading_path=" / ".join(chunk.heading),
                        content=chunk.content,
                        token_count=max(1, len(chunk.content.split())),
                        content_type=chunk.content_type,
                        split_reason=chunk.split_reason,
                        chunk_hash=chunk.chunk_hash,
                        metadata_json=json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
                    )
                if artifact_store is not None:
                    try:
                        bundle = store_artifact_bundle(
                            artifact_store,
                            tenant_id=request.tenant_id,
                            shop_id=request.shop_id,
                            document_version_id=spec["version_id"],
                            parser_version="grpc-local-parser-v1",
                            chunk_rule_version="structured-v2",
                            artifacts=build_ingestion_artifacts(
                                raw=package["uploaded"][spec["path"]],
                                elements=elements,
                                chunks=chunks,
                                parse_report={
                                    "status": "PASS",
                                    "document_id": spec["document_id"],
                                    "document_version_id": spec["version_id"],
                                    "source_hash": spec["source_hash"],
                                    "parser_version": "grpc-local-parser-v1",
                                    "chunk_rule_version": "structured-v2",
                                    "element_count": len(elements),
                                    "chunk_count": len(chunks),
                                    "real_service_acceptance": False,
                                },
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001 - provider/storage details stay redacted
                        fail(f"artifact bundle could not be stored: {type(exc).__name__}")
                    bundles.append(bundle)
                    _bundle_reference(parsed, bundle)
                dataset_rows.append({
                    "document_id": spec["document_id"],
                    "source_hash": spec["source_hash"],
                    "chunks": len(chunks),
                })
        canonical = json.dumps(
            {"manifest_sha256": package["manifest_sha256"], "documents": dataset_rows},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        response.dataset_sha256 = hashlib.sha256(canonical).hexdigest()
        return response

    def DeleteArtifactBundle(self, request, context):
        keys = _artifact_cleanup_keys(request, context)
        try:
            store = _artifact_store()
        except (OSError, RuntimeError, ValueError):
            _fail(context, "artifact cleanup storage is unavailable")
        if store is None:
            _fail(context, "artifact cleanup storage is unavailable")
        complete = _delete_keys(store, keys)
        return rag_pb2.DeleteArtifactBundleResponse(
            complete=complete,
            error_code="" if complete else "cleanup_incomplete",
        )


def register(server) -> None:
    rag_pb2_grpc.add_IngestServiceServicer_to_server(IngestService(), server)
