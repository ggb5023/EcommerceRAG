"""Validate and parse bounded manifest packages for the Go gateway."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath

import grpc
import yaml

from app.ingest.pipeline import chunk_elements, parser_for
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
                    chunks = chunk_elements(elements)
                except (OSError, UnicodeError, ValueError, KeyError, RuntimeError):
                    _fail(context, "document could not be parsed")
                if not chunks:
                    _fail(context, "document produced no content chunks")
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
                    parsed.chunks.add(
                        chunk_index=chunk_index,
                        section_seq=chunk.section_seq,
                        section_chunk_index=chunk.chunk_index,
                        char_start=0,
                        char_end=len(chunk.content),
                        title=chunk.title,
                        heading_path=" / ".join(chunk.heading),
                        content=chunk.content,
                        token_count=max(1, len(chunk.content.split())),
                        content_type="text",
                        split_reason=chunk.split_reason,
                        chunk_hash=chunk.chunk_hash,
                        metadata_json=json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
                    )
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


def register(server) -> None:
    rag_pb2_grpc.add_IngestServiceServicer_to_server(IngestService(), server)
