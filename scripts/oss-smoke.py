#!/usr/bin/env python3
"""Run an opt-in, redacted smoke against the configured Dev OSS bucket.

Without ``--live`` this command only checks the restricted configuration and
SDK availability. The live path uses one uniquely named test object, verifies
the adapter contract, and deletes that object before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from app.storage import (
    OSS_CONFIG_PATH,
    AlibabaOSSObjectStore,
    ObjectStoreError,
    load_oss_config,
    object_key,
)


def _config_summary(config) -> dict[str, object]:
    return {
        "bucket": config.bucket,
        "region": config.region,
        "endpoint_host": urlsplit(config.endpoint).hostname,
    }


def _safe_fetch(url: str) -> bytes:
    """Fetch a signed test object without logging the URL or response."""

    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read()


def _local_gate(config_path: Path) -> int:
    try:
        config = load_oss_config(config_path)
    except ObjectStoreError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return 3
    try:
        import oss2  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        print(
            "NOT_RUN/CONFIG_BLOCKED OSS SDK is unavailable "
            + json.dumps(_config_summary(config), sort_keys=True)
        )
        return 3
    _ = oss2
    print(
        "NOT_RUN live flag is required; no OSS request was made "
        + json.dumps(_config_summary(config), sort_keys=True)
    )
    return 3


def _live(
    config_path: Path,
    *,
    store_factory=AlibabaOSSObjectStore,
    fetcher=_safe_fetch,
    sleeper=time.sleep,
) -> int:
    try:
        config = load_oss_config(config_path)
    except ObjectStoreError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return 3
    if config.bucket != "ecommercerag-dev" or config.region != "cn-beijing":
        print("BLOCKED live OSS smoke is restricted to ecommercerag-dev/cn-beijing")
        return 3
    try:
        store = store_factory(config=config)
    except ObjectStoreError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return 3

    token = secrets.token_hex(8)
    content = f"ecommerce-rag OSS Dev smoke {token}\n".encode("ascii")
    digest = hashlib.sha256(content).hexdigest()
    key = object_key(
        "ecr-smoke-tenant",
        "ecr-smoke-shop",
        f"p2-{token}",
        "p2-live-smoke",
        digest,
    )
    checks: dict[str, bool] = {
        "upload": False,
        "head_hash": False,
        "download_hash": False,
        "signed_download": False,
        "expired_signature_rejected": False,
        "delete": False,
        "deleted_unreadable": False,
        "live_operation_error": True,
    }
    cleanup_error = False
    try:
        metadata = store.put_bytes(
            key, content, content_type="text/plain", expected_sha256=digest
        )
        checks["upload"] = metadata.sha256 == digest and metadata.size_bytes == len(content)

        head = store.head(key)
        checks["head_hash"] = head.sha256 == digest and head.size_bytes == len(content)

        downloaded, downloaded_meta = store.get_bytes(key, expected_sha256=digest)
        checks["download_hash"] = (
            downloaded == content and downloaded_meta.sha256 == digest
        )

        signed = fetcher(store.presign_get(key, expires_s=60))
        checks["signed_download"] = hashlib.sha256(signed).hexdigest() == digest

        short_url = store.presign_get(key, expires_s=1)
        sleeper(2)
        try:
            fetcher(short_url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            checks["expired_signature_rejected"] = True
    except Exception:  # noqa: BLE001 - redact vendor errors
        checks["live_operation_error"] = False
    finally:
        try:
            store.delete(key)
            checks["delete"] = True
            try:
                store.head(key)
            except ObjectStoreError:
                checks["deleted_unreadable"] = True
        except Exception:  # noqa: BLE001 - cleanup status is reported only
            cleanup_error = True

    passed = all(checks.values()) and not cleanup_error
    report = {
        "status": "PASS" if passed else "FAIL",
        "bucket": config.bucket,
        "region": config.region,
        "endpoint_host": urlsplit(config.endpoint).hostname,
        "object_sha256": digest,
        "object_size_bytes": len(content),
        "checks": checks,
        "cleanup_error": cleanup_error,
        "raw_content_saved": False,
        "secret_values_saved": False,
        "real_service_acceptance": False,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=OSS_CONFIG_PATH)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    return _live(args.env) if args.live else _local_gate(args.env)


if __name__ == "__main__":
    raise SystemExit(main())
