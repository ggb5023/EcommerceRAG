#!/usr/bin/env python3
"""Run an isolated MinerU URL smoke over the restricted PDF manifest."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
import time
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from app.providers.config import ProviderConfigError
from app.providers.contracts import ProviderError
from app.providers.mineru import MinerUClient, load_mineru_config

PDF_ROOT = pathlib.Path("/var/lib/ecommerce-rag/real-docs/real-docs-v1")


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_mineru_config(args.env)
    except ProviderConfigError as error:
        print(f"CONFIG_BLOCKED {error}")
        return 3
    manifest = json.loads((args.root / "manifest.json").read_text(encoding="utf-8"))
    client = MinerUClient(replace(config, poll_timeout_s=args.poll_timeout))
    skip_ids = set(args.skip_document_id)
    only_ids = set(args.only_document_id)
    rows = []
    for index, source in enumerate(manifest.get("sources", [])):
        if args.limit and index >= args.limit:
            break
        document_id = source["document_id"]
        if document_id in skip_ids or (only_ids and document_id not in only_ids):
            continue
        path = args.root / "raw" / source["file_name"]
        row = {"document_id": document_id, "file_name": path.name,
               "input_sha256": sha256(path), "status": "FAILED"}
        started = time.monotonic()
        try:
            task_id = await client.submit_url(source["source_url"], data_id=source["document_id"])
            row["task_id"] = task_id
            result = await client.wait(task_id)
            row["status"] = "PASS"
            data = result.get("data", result)
            if isinstance(data, dict):
                row["result_url_present"] = bool(data.get("full_zip_url") or data.get("zip_url") or data.get("result_url"))
        except (ProviderError, OSError, RuntimeError, TypeError, ValueError) as error:
            row["error_code"] = getattr(error, "code", "unexpected_error")
        row["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        rows.append(row)
        if args.first_success and row["status"] == "PASS":
            break
    output = {"report_version": "mineru-smoke-v1", "real_service_acceptance": False,
              "documents": rows, "raw_result_saved": False}
    out = args.output or args.root / "reports" / "mineru-smoke.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "documents": len(rows),
                      "passed": sum(row["status"] == "PASS" for row in rows)}))
    return 0 if rows and all(row["status"] == "PASS" for row in rows) else 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument("--root", type=pathlib.Path, default=PDF_ROOT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--first-success", action="store_true")
    parser.add_argument("--poll-timeout", type=float, default=60.0)
    parser.add_argument("--skip-document-id", action="append", default=[],
                        help="Skip a manifest document ID; may be repeated")
    parser.add_argument("--only-document-id", action="append", default=[],
                        help="Process only this manifest document ID; may be repeated")
    parser.add_argument("--output", type=pathlib.Path,
                        help="Write the smoke report to this path instead of the default")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
