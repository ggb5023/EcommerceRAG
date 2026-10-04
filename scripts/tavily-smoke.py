#!/usr/bin/env python3
"""Independent Tavily smoke; prints metadata only and never stores content."""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from app.providers.config import ProviderConfigError  # noqa: E402
from app.providers.tavily import TavilyProvider, load_tavily_config  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    try:
        config = load_tavily_config(args.env)
    except ProviderConfigError as error:
        print(f"CONFIG_BLOCKED {error}")
        return 3
    started = time.monotonic()
    try:
        result = await TavilyProvider(config).search(args.query)
    except Exception as error:  # redact vendor/network details
        code = getattr(error, "code", "unexpected_error")
        report = {"status": "FAILED", "error_code": code, "real_service_acceptance": False, "raw_content_saved": False}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
        return 4
    report = {
        "status": result.status,
        "result_count": len(result.results),
        "request_id_present": bool(result.request_id),
        "usage_present": result.usage is not None,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "result_domains": sorted({r.url.split("/", 3)[2] for r in result.results}),
        "real_service_acceptance": False,
        "raw_content_saved": False,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument("--query", default="Raspberry Pi Pico specifications")
    parser.add_argument("--output", type=pathlib.Path,
                        default=pathlib.Path("/var/lib/ecommerce-rag/real-docs/reports/tavily-smoke.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
