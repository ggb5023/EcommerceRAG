#!/usr/bin/env python3
"""Exercise the multi-artifact object-storage contract.

The default path is a temporary filesystem smoke.  ``--live`` is required for
the restricted Alibaba OSS Dev bucket and creates only short-lived test
objects.  Output contains hashes and sizes, never artifact contents.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from app.ingest.artifacts import ArtifactInput, read_manifest, store_artifact_bundle
from app.storage import (
    OSS_CONFIG_PATH,
    AlibabaOSSObjectStore,
    FilesystemObjectStore,
    ObjectStoreError,
    load_oss_config,
)


def _inputs(token: str) -> tuple[ArtifactInput, ...]:
    return (
        ArtifactInput("raw", f"synthetic raw artifact {token}\n".encode(), "text/plain"),
        ArtifactInput(
            "parsed",
            json.dumps({"element_count": 2, "source": "synthetic"}, sort_keys=True).encode(),
            "application/json",
        ),
        ArtifactInput(
            "chunks",
            b'{"chunk_id":"synthetic-chunk-1","source_position":{"line":1}}\n',
            "application/jsonl",
        ),
        ArtifactInput(
            "parse-report",
            json.dumps({"status": "PASS", "warnings": 0}, sort_keys=True).encode(),
            "application/json",
        ),
    )


def _run(store, *, backend: str, cleanup: bool = True, output: Path | None = None) -> int:
    token = secrets.token_hex(8)
    inputs = _inputs(token)
    bundle = None
    cleanup_error = False
    checks = {
        "bundle_written": False,
        "manifest_verified": False,
        "artifacts_verified": False,
        "cleanup": not cleanup,
    }
    try:
        bundle = store_artifact_bundle(
            store,
            tenant_id="ecr-smoke-tenant",
            shop_id="ecr-smoke-shop",
            document_version_id=f"bundle-{token}",
            parser_version="parser-v1",
            chunk_rule_version="structured-v2",
            artifacts=inputs,
        )
        checks["bundle_written"] = len(bundle.records) == len(inputs)
        manifest = read_manifest(store, bundle)
        checks["manifest_verified"] = (
            manifest.get("artifact_set_sha256") == bundle.artifact_set_sha256
            and manifest.get("real_service_acceptance") is False
        )
        checks["artifacts_verified"] = all(
            store.get_bytes(record.object_key, expected_sha256=record.sha256)[1].size_bytes
            == record.size_bytes
            for record in bundle.records
        )
    except Exception:  # noqa: BLE001 - vendor details must not escape
        checks["bundle_written"] = False
    finally:
        if cleanup and bundle is not None:
            for record in reversed(bundle.all_records):
                try:
                    store.delete(record.object_key)
                except Exception:  # noqa: BLE001 - report only a redacted flag
                    cleanup_error = True
            checks["cleanup"] = not cleanup_error

    records = []
    if bundle is not None:
        records = [
            {
                "artifact_type": record.artifact_type,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
            }
            for record in bundle.records
        ]
    report = {
        "status": "PASS" if all(checks.values()) and not cleanup_error else "FAIL",
        "backend": backend,
        "artifact_count": len(records),
        "artifacts": records,
        "checks": checks,
        "cleanup_error": cleanup_error,
        "raw_content_saved": False,
        "real_service_acceptance": False,
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 4


def _live(config_path: Path, output: Path | None = None) -> int:
    try:
        config = load_oss_config(config_path)
    except ObjectStoreError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return 3
    if config.bucket != "ecommercerag-dev" or config.region != "cn-beijing":
        print("BLOCKED live artifact smoke is restricted to ecommercerag-dev/cn-beijing")
        return 3
    try:
        store = AlibabaOSSObjectStore(config=config)
    except ObjectStoreError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return 3
    return _run(store, backend="aliyun-oss", output=output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env", type=Path, default=OSS_CONFIG_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.live:
        return _live(args.env, args.output)
    with tempfile.TemporaryDirectory(prefix="ecr-artifact-smoke-") as directory:
        return _run(FilesystemObjectStore(directory), backend="filesystem", output=args.output)


if __name__ == "__main__":
    raise SystemExit(main())
