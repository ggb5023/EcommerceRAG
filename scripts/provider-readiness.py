#!/usr/bin/env python3
"""Produce a redacted, offline readiness report for the Provider profile.

This command only parses the explicitly supplied providers.env file. It never
contacts DashScope, never merges process environment values, and never emits a
model ID, endpoint URL, API key, request body, or provider response.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from app.providers.config import ProviderConfigError, load_provider_config

SLOTS = ("control", "embedding", "rerank", "generation")


def _endpoint_host(endpoint: str) -> str | None:
    parsed = urlsplit(endpoint)
    return parsed.hostname


def build_report(path: Path) -> tuple[dict[str, object], int]:
    try:
        config = load_provider_config(path)
    except ProviderConfigError as exc:
        status = exc.code if exc.code in {"CONFIG_BLOCKED", "CONFIG_FAIL"} else "CONFIG_BLOCKED"
        report = {
            "status": status,
            "config_path": str(path),
            "profile": None,
            "slots": {slot: {"configured": False} for slot in SLOTS},
            "online_smoke": "NOT_RUN",
            "real_service_acceptance": False,
            "secret_values_saved": False,
            "error_code": status,
        }
        return report, 3 if status == "CONFIG_BLOCKED" else 2

    report = {
        "status": "MOCK_ONLY" if config.profile == "mock" else "READY_FOR_INDEPENDENT_SMOKE",
        "config_path": str(path),
        "profile": config.profile,
        "endpoint_host": _endpoint_host(config.endpoint),
        "region": config.region,
        "embedding_dimensions": config.embedding_dimensions,
        "quota_rpm": config.quota_rpm,
        "timeout_s": config.timeout_s,
        "slots": {
            slot: {"configured": bool(config.models[slot])} for slot in SLOTS
        },
        "online_smoke": "NOT_RUN",
        "real_service_acceptance": False,
        "secret_values_saved": False,
    }
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("/etc/ecommerce-rag/providers.env"))
    args = parser.parse_args()
    report, exit_code = build_report(args.env)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
