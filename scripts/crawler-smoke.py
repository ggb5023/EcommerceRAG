#!/usr/bin/env python3
"""Run an explicit test-only smoke over registered official documentation URLs.

The registry remains pending and is never activated by this command. Live mode
stores bounded snapshots only under a restricted external directory; reports
contain metadata and hashes, never page bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from app.web.crawler import CrawlError, SourcePolicy, fetch_snapshot

REGISTRY = ROOT / "data/web/source-registry-v1.json"
DEFAULT_ROOT = Path("/var/lib/ecommerce-rag/real-docs/crawler-smoke-v1")
OFFICIAL_IDS = (
    "raspberrypi-official-docs",
    "shopify-developer-docs",
    "woocommerce-official-docs",
)


def _load_sources() -> list[dict[str, object]]:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    by_id = {item["source_id"]: item for item in payload["sources"]}
    return [by_id[source_id] for source_id in OFFICIAL_IDS]


def _policy(source: dict[str, object]) -> SourcePolicy:
    limits = source["crawl_limits"]
    return SourcePolicy(
        source_id=str(source["source_id"]),
        allowed_hosts=tuple(str(value) for value in source["base_domains"]),
        allowed_paths=tuple(str(value) for value in source["allowed_paths"]),
        max_bytes=int(limits["max_bytes_per_page"]),
        timeout_s=10,
        max_redirects=3,
        retries=0,
        purpose="test_only_official_document_parser_smoke",
        authorization="test_only",
        owner="engineering",
        retention_days=1,
        policy_version=str(source["policy_version"]),
        allowed_content_types=tuple(str(value) for value in source["content_types"]),
    )


def _local_gate() -> int:
    sources = _load_sources()
    print(
        "NOT_RUN live flag is required; registry remains pending "
        + json.dumps(
            {
                "source_count": len(sources),
                "active_count": sum(source.get("status") == "active" for source in sources),
                "network_requests": 0,
                "real_service_acceptance": False,
            },
            sort_keys=True,
        )
    )
    return 3


def _live(root: Path) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    sources = _load_sources()
    if any(source.get("status") != "pending_review" for source in sources):
        print("BLOCKED crawler smoke requires all registered sources to remain pending_review")
        return 3
    results: list[dict[str, object]] = []
    for source in sources:
        source_id = str(source["source_id"])
        urls = source.get("fixed_urls")
        url = str(urls[0]) if isinstance(urls, list) and urls else ""
        result: dict[str, object] = {
            "source_id": source_id,
            "url_host": urlsplit(url).hostname,
            "status": "FAIL",
            "real_service_acceptance": False,
        }
        try:
            manifest = fetch_snapshot(url, _policy(source), root)
            result.update(
                {
                    "status": "PASS",
                    "content_type": manifest.get("content_type"),
                    "size_bytes": manifest.get("size_bytes"),
                    "sha256": manifest.get("sha256"),
                    "snapshot_path_external": True,
                }
            )
        except CrawlError as exc:
            result.update({"status": "FAIL", "error_code": exc.code})
        except Exception:  # noqa: BLE001 - redact network/library errors
            result.update({"status": "FAIL", "error_code": "unexpected_error"})
        results.append(result)
    passed = bool(results) and all(result["status"] == "PASS" for result in results)
    report = {
        "status": "PASS" if passed else "FAIL",
        "source_count": len(results),
        "active_count": 0,
        "network_requests": len(results) * 2,
        "registry_changed": False,
        "raw_snapshots_external_only": True,
        "sources": results,
        "real_service_acceptance": False,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    return _live(args.root) if args.live else _local_gate()


if __name__ == "__main__":
    raise SystemExit(main())
