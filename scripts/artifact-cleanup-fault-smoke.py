#!/usr/bin/env python3
"""Exercise cleanup fault injection through a real synthetic gRPC process.

This is a local negative smoke only.  It starts the parser service with a
test-only failure budget, calls the cleanup RPC for two bundles, and verifies
that one injected incomplete response does not prevent later cleanup.  No
database, online provider, crawler, or M1 service is used.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from app.ingest.artifacts import ArtifactInput, store_artifact_bundle
from app.storage import FilesystemObjectStore, ObjectStoreError
from rag.v1 import rag_pb2, rag_pb2_grpc


def _free_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{sock.getsockname()[1]}"


def _request(bundle):
    reference = rag_pb2.ArtifactBundleReference(
        document_version_id=bundle.document_version_id,
        manifest_object_key=bundle.manifest.object_key,
        manifest_sha256=bundle.manifest.sha256,
        artifact_set_sha256=bundle.artifact_set_sha256,
        real_service_acceptance=False,
        artifacts=[
            rag_pb2.ArtifactRecord(
                artifact_type=record.artifact_type,
                object_key=record.object_key,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                content_type=record.content_type,
            )
            for record in bundle.records
        ],
    )
    return rag_pb2.DeleteArtifactBundleRequest(
        tenant_id=bundle.tenant_id,
        shop_id=bundle.shop_id,
        artifact_bundle=reference,
    )


def _assert_missing(store: FilesystemObjectStore, bundle) -> None:
    for record in bundle.all_records:
        try:
            store.head(record.object_key)
        except ObjectStoreError:
            continue
        raise RuntimeError("cleanup left an artifact object behind")


def run(python: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ecr-cleanup-fault-") as directory:
        root = Path(directory)
        store = FilesystemObjectStore(root)
        inputs = (
            ArtifactInput("raw", b"synthetic raw"),
            ArtifactInput("parsed", b"synthetic parsed", "application/json"),
            ArtifactInput("chunks", b"synthetic chunks", "application/jsonl"),
            ArtifactInput("parse-report", b"synthetic report", "application/json"),
        )
        first = store_artifact_bundle(
            store, tenant_id="fault-tenant", shop_id="fault-shop",
            document_version_id="v-11111111111111111111", artifacts=inputs,
        )
        second = store_artifact_bundle(
            store, tenant_id="fault-tenant", shop_id="fault-shop",
            document_version_id="v-22222222222222222222", artifacts=inputs,
        )

        address = _free_address()
        environment = os.environ.copy()
        environment.update({
            "PYTHONPATH": str(PYTHON_ROOT),
            "APP_ENV": "test",
            "RAG_PROFILE": "synthetic_import_mock",
            "SYNTHETIC_MANIFEST": str(ROOT / "data/synthetic/ecommerce-demo-v1/manifest.yaml"),
            "INGEST_ARTIFACT_BUNDLE_ROOT": str(root),
            "INGEST_TEST_DELETE_FAILURES": "1",
            "GRPC_ADDR": address,
        })
        process = subprocess.Popen(
            [str(python), "-m", "app.server"],
            cwd=PYTHON_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            channel = grpc.insecure_channel(address)
            grpc.channel_ready_future(channel).result(timeout=10)
            client = rag_pb2_grpc.IngestServiceStub(channel)
            injected = client.DeleteArtifactBundle(_request(first), timeout=5)
            continued = client.DeleteArtifactBundle(_request(second), timeout=5)
            recovered = client.DeleteArtifactBundle(_request(first), timeout=5)
            if injected.complete or injected.error_code != "test_injected_cleanup_failure":
                raise RuntimeError("first cleanup call was not the injected incomplete response")
            if not continued.complete or not recovered.complete:
                raise RuntimeError("cleanup did not recover after the injected response")
            remaining_first = sum(
                1 for record in first.all_records
                if (root / record.object_key).exists()
            )
            remaining_second = sum(
                1 for record in second.all_records
                if (root / record.object_key).exists()
            )
            if remaining_first or remaining_second:
                raise RuntimeError("process cleanup left objects behind")
            channel.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    return {
        "status": "PASS",
        "profile": "synthetic_import_mock",
        "failure_budget": 1,
        "bundles_called": 3,
        "first_response": "injected_incomplete",
        "later_cleanup": "continued_and_recovered",
        "raw_content_saved": False,
        "real_service_acceptance": False,
        "online_provider_called": False,
        "public_crawler_called": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, default=ROOT / "python/.venv/bin/python")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.python.is_file():
        print("NOT_RUN/CONFIG_BLOCKED python virtualenv is unavailable", file=sys.stderr)
        return 3
    try:
        report = run(args.python)
    except Exception as exc:  # noqa: BLE001 - report only a redacted failure
        print(f"FAIL cleanup fault smoke: {type(exc).__name__}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
