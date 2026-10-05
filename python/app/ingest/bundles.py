"""Build a content-addressed artifact bundle for a validated manifest.

This module is an offline ingestion boundary.  It intentionally does not
write PostgreSQL, activate a document version, or call a model provider.  A
whole manifest run is atomic from the caller's point of view: if one content
document fails, objects written for earlier documents in that run are
removed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.ingest.artifacts import (
    ArtifactBundle,
    ArtifactBundleError,
    build_ingestion_artifacts,
    store_artifact_bundle,
)
from app.ingest.cli import ValidationResult, validate_manifest
from app.ingest.pipeline import chunk_elements_v2, parser_for
from app.storage import ObjectStore


class BundleRunError(RuntimeError):
    """A manifest could not be converted into a complete artifact run."""


def _version_id(source_hash: str, pipeline_version: str) -> str:
    return "v-" + hashlib.sha256(
        f"{source_hash}{pipeline_version}".encode()
    ).hexdigest()[:16]


def _delete_bundles(store: ObjectStore, bundles: Iterable[ArtifactBundle]) -> bool:
    clean = True
    for bundle in reversed(tuple(bundles)):
        for record in reversed(bundle.all_records):
            try:
                store.delete(record.object_key)
            except Exception:  # noqa: BLE001 - report cleanup only as a flag
                clean = False
    return clean


def bundle_dataset(
    result: ValidationResult,
    store: ObjectStore,
    *,
    parser_version: str = "local-parser-v1",
    chunk_rule_version: str = "structured-v2",
) -> dict[str, Any]:
    """Parse and persist all content documents in a validated manifest.

    YAML control fixtures are not customer evidence and are reported as
    ``SKIPPED_CONTROL``.  The returned report contains no source text; it is
    safe to persist as an operator report.  ``result`` must already be valid
    so callers cannot accidentally bundle unverified files.
    """

    if not result.ok:
        raise BundleRunError("manifest validation failed; bundle refused")

    bundles: list[ArtifactBundle] = []
    documents: list[dict[str, Any]] = []
    try:
        for document_id, info in sorted(result.files.items()):
            version_id = _version_id(info["sha256"], result.manifest["pipeline_version"])
            base = {
                "document_id": document_id,
                "document_version_id": version_id,
                "format": info["format"],
                "source_hash": info["sha256"],
                "tenant_id": info["tenant_id"],
                "shop_id": info["shop_id"],
            }
            if info["format"] == "yaml":
                documents.append({**base, "status": "SKIPPED_CONTROL", "artifact_count": 0})
                continue

            path = result.root / info["path"]
            metadata = {
                **info,
                "title": document_id,
                "source_hash": info["sha256"],
            }
            try:
                elements = parser_for(info["format"]).parse(
                    path,
                    document_id=document_id,
                    version_id=version_id,
                    metadata=metadata,
                )
                chunks = chunk_elements_v2(elements, rule_version=chunk_rule_version)
                artifacts = build_ingestion_artifacts(
                    raw=path.read_bytes(),
                    elements=elements,
                    chunks=chunks,
                    parse_report={
                        "status": "PASS",
                        "document_id": document_id,
                        "document_version_id": version_id,
                        "source_hash": info["sha256"],
                        "parser_version": parser_version,
                        "chunk_rule_version": chunk_rule_version,
                        "element_count": len(elements),
                        "chunk_count": len(chunks),
                        "real_service_acceptance": False,
                    },
                )
                bundle = store_artifact_bundle(
                    store,
                    tenant_id=info["tenant_id"],
                    shop_id=info["shop_id"],
                    document_version_id=version_id,
                    parser_version=parser_version,
                    chunk_rule_version=chunk_rule_version,
                    artifacts=artifacts,
                )
            except (ArtifactBundleError, OSError, UnicodeError, ValueError, KeyError) as exc:
                raise BundleRunError(f"{document_id}: {type(exc).__name__}") from exc
            bundles.append(bundle)
            documents.append({
                **base,
                "status": "PASS",
                "artifact_count": len(bundle.records),
                "artifact_set_sha256": bundle.artifact_set_sha256,
                "manifest_sha256": bundle.manifest.sha256,
                "chunks": len(chunks),
                "elements": len(elements),
            })
    except Exception as exc:
        if not _delete_bundles(store, bundles):
            raise BundleRunError("bundle failed and cleanup was incomplete") from exc
        if isinstance(exc, BundleRunError):
            raise
        raise BundleRunError("bundle run failed") from exc

    return {
        "report_version": "synthetic-ingestion-bundle-v1",
        "dataset_id": result.manifest["dataset_id"],
        "pipeline_version": result.manifest["pipeline_version"],
        "manifest_sha256": result.manifest_sha256,
        "dataset_sha256": result.dataset_sha256,
        "parser_version": parser_version,
        "chunk_rule_version": chunk_rule_version,
        "documents": documents,
        "summary": {
            "documents": len(documents),
            "bundled": sum(item["status"] == "PASS" for item in documents),
            "skipped_control": sum(item["status"] == "SKIPPED_CONTROL" for item in documents),
            "failed": 0,
        },
        "raw_content_saved": False,
        "real_service_acceptance": False,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write a metadata-only report atomically."""

    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def run_manifest_bundle(
    manifest: Path,
    store: ObjectStore,
    *,
    output: Path | None = None,
    parser_version: str = "local-parser-v1",
    chunk_rule_version: str = "structured-v2",
) -> dict[str, Any]:
    """Validate, bundle, and optionally write a metadata-only report."""

    result = validate_manifest(manifest)
    report = bundle_dataset(
        result,
        store,
        parser_version=parser_version,
        chunk_rule_version=chunk_rule_version,
    )
    if output is not None:
        write_report(report, output)
    return report
