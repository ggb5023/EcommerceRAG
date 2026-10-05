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

from app.ingest.artifacts import (
    ArtifactBundleError,
    ArtifactInput,
    read_manifest,
    store_artifact_bundle,
)
from app.storage import (
    OSS_CONFIG_PATH,
    AlibabaOSSObjectStore,
    FilesystemObjectStore,
    ObjectStore,
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


class _FailingStore:
    """Fail one write while preserving the ObjectStore surface for rollback tests."""

    def __init__(self, delegate: ObjectStore, fail_on_put: int):
        self.delegate = delegate
        self.fail_on_put = fail_on_put
        self.put_count = 0

    def put_bytes(self, key, data, *, content_type, expected_sha256):
        self.put_count += 1
        if self.put_count == self.fail_on_put:
            raise ObjectStoreError("simulated storage failure")
        return self.delegate.put_bytes(
            key,
            data,
            content_type=content_type,
            expected_sha256=expected_sha256,
        )

    def get_bytes(self, key, *, expected_sha256=None):
        return self.delegate.get_bytes(key, expected_sha256=expected_sha256)

    def delete(self, key):
        return self.delegate.delete(key)

    def head(self, key):
        return self.delegate.head(key)

    def presign_get(self, key, *, expires_s=300):
        return self.delegate.presign_get(key, expires_s=expires_s)


def _negative_checks() -> dict[str, bool]:
    """Exercise rollback and manifest tamper rejection without network access."""

    checks = {"rollback": False, "tamper_rejected": False}
    with tempfile.TemporaryDirectory(prefix="ecr-artifact-negative-") as directory:
        root = Path(directory)
        filesystem = FilesystemObjectStore(root)
        failing = _FailingStore(filesystem, fail_on_put=2)
        try:
            store_artifact_bundle(
                failing,
                tenant_id="ecr-negative-tenant",
                shop_id="ecr-negative-shop",
                document_version_id="rollback-v1",
                artifacts=(ArtifactInput("raw", b"raw"), ArtifactInput("parsed", b"parsed")),
            )
        except ArtifactBundleError:
            checks["rollback"] = not any(path.is_file() for path in root.rglob("*"))

        bundle = store_artifact_bundle(
            filesystem,
            tenant_id="ecr-negative-tenant",
            shop_id="ecr-negative-shop",
            document_version_id="tamper-v1",
            artifacts=(ArtifactInput("report", b"report", "application/json"),),
        )
        Path(root / bundle.manifest.object_key).write_bytes(b"tampered")
        try:
            read_manifest(filesystem, bundle)
        except ArtifactBundleError:
            checks["tamper_rejected"] = True
        for record in bundle.all_records:
            filesystem.delete(record.object_key)
    return checks


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
    negative_checks = _negative_checks() if backend == "filesystem" else None
    report = {
        "status": "PASS" if all(checks.values()) and not cleanup_error else "FAIL",
        "backend": backend,
        "artifact_count": len(records),
        "artifacts": records,
        "checks": checks,
        "negative_checks": negative_checks,
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
    if negative_checks is not None and not all(negative_checks.values()):
        report["status"] = "FAIL"
        if output is not None:
            output.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
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
