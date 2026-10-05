#!/usr/bin/env python3
"""Audit crawler source gates without making network requests.

This command is intentionally a local registry audit. It does not fetch
robots.txt, resolve DNS, call Tavily, or write snapshots. A passing audit only
means the source policies are complete enough to await human approval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from scripts.validate_source_registry import DEFAULT_ROOT, validate
except ModuleNotFoundError:  # direct execution: scripts/ is on sys.path
    from validate_source_registry import DEFAULT_ROOT, validate

EXPECTED_OFFICIAL = {
    "raspberrypi-official-docs",
    "shopify-developer-docs",
    "woocommerce-official-docs",
}
ALLOWED_NON_SOURCE = {"tavily-discovery-candidates"}
CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def audit(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    registry_report = validate(path)
    if registry_report["status"] != "PASS":
        errors.extend(str(item) for item in registry_report["errors"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"registry_read_failed:{type(exc).__name__}")
        payload = {}

    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    by_id = {
        item.get("source_id"): item
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    }
    missing = sorted(EXPECTED_OFFICIAL - set(by_id))
    if missing:
        errors.append(f"missing_official_source:{','.join(missing)}")
    unexpected = sorted(set(by_id) - EXPECTED_OFFICIAL - ALLOWED_NON_SOURCE)
    if unexpected:
        errors.append(f"unexpected_source:{','.join(unexpected)}")

    for source_id in sorted(EXPECTED_OFFICIAL):
        item = by_id.get(source_id)
        if not item:
            continue
        prefix = f"{source_id}"
        if item.get("status") != "pending_review":
            errors.append(f"{prefix}:status_must_be_pending_review")
        if not item.get("fixed_urls"):
            errors.append(f"{prefix}:fixed_urls_required")
        terms = item.get("terms_review")
        robots = item.get("robots_policy")
        if not isinstance(terms, dict) or terms.get("status") != "pending":
            errors.append(f"{prefix}:terms_review_must_be_pending")
        if not isinstance(robots, dict) or robots.get("status") != "pending":
            errors.append(f"{prefix}:robots_policy_must_be_pending")
        if item.get("authorization") != "pending":
            errors.append(f"{prefix}:authorization_must_be_pending")
        limits = item.get("crawl_limits")
        if not isinstance(limits, dict):
            errors.append(f"{prefix}:crawl_limits_missing")
        else:
            for key in ("max_pages_per_run", "max_bytes_per_page", "max_total_bytes_per_run"):
                if not _positive_int(limits.get(key)):
                    errors.append(f"{prefix}:{key}_invalid")
        content_types = item.get("content_types")
        if (
            not isinstance(content_types, list)
            or not content_types
            or any(value not in CONTENT_TYPES for value in content_types)
        ):
            errors.append(f"{prefix}:content_types_invalid")
        retention = item.get("retention_policy")
        if not isinstance(retention, dict) or retention.get("status") != "pending":
            errors.append(f"{prefix}:retention_policy_must_be_pending")
        refresh = item.get("refresh_policy")
        if not isinstance(refresh, dict) or refresh.get("trigger") != "manual":
            errors.append(f"{prefix}:refresh_policy_must_be_manual")

    return {
        "status": "PASS" if not errors else "FAIL",
        "source_count": len(sources) if isinstance(sources, list) else 0,
        "official_source_count": len(EXPECTED_OFFICIAL & set(by_id)),
        "active_count": sum(
            item.get("status") == "active" for item in sources if isinstance(item, dict)
        ),
        "network_requests": 0,
        "public_crawl": "NOT_RUN",
        "release_gate": "PENDING_REVIEW",
        "real_service_acceptance": False,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    report = audit(args.path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
