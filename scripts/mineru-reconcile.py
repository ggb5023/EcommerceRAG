#!/usr/bin/env python3
"""Poll an existing MinerU task without submitting a duplicate."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from app.providers.config import ProviderConfigError
from app.providers.contracts import ProviderError
from app.providers.mineru import (
    MinerUClient,
    load_mineru_config,
    result_url,
    safe_extract_zip,
    sha256_bytes,
)


def _write_json_atomic(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_mineru_config(args.env)
    except ProviderConfigError as error:
        print(f"CONFIG_BLOCKED {error}")
        return 3
    client = MinerUClient(replace(config, poll_timeout_s=args.poll_timeout))
    try:
        result = await client.wait(args.task_id)
        url = result_url(result)
        if not url:
            raise ProviderError("result_url_missing", "MinerU result URL is missing", provider="mineru")
        raw = await client.download_result(url)
        report = {
            "task_id": args.task_id,
            "status": "PASS",
            "result_sha256": sha256_bytes(raw),
            "result_size_bytes": len(raw),
            "result_url_present": True,
            "real_service_acceptance": False,
        }
        if args.artifact_root:
            artifact_root = args.artifact_root.resolve()
            parsed_root = artifact_root / args.task_id
            paths = safe_extract_zip(raw, parsed_root)
            report["extracted_files"] = [
                {"path": str(path.relative_to(parsed_root)), "size_bytes": path.stat().st_size}
                for path in paths
            ]
            report["artifact_root"] = str(parsed_root)
            meta = parsed_root / "meta.json"
            _write_json_atomic(meta, report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ProviderError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(json.dumps({"task_id": args.task_id, "status": "FAILED",
                          "error_code": getattr(error, "code", "unexpected_error"),
                          "real_service_acceptance": False}))
        return 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument("--poll-timeout", type=float, default=60.0)
    parser.add_argument("--artifact-root", type=pathlib.Path)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
