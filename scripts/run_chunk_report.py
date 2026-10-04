#!/usr/bin/env python3
"""Write a metadata-only chunk report from the local parser report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path("/var/lib/ecommerce-rag/real-docs/source-fixtures-v1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    source = args.root / "reports" / "parse-report.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    chunks = []
    for report in data["reports"]:
        if report["parse_status"] != "PASS":
            continue
        for item in report["chunks"]:
            chunks.append({"fixture_id": report["fixture_id"], **item})
    canonical = json.dumps(chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    output = {
        "report_version": "source-fixtures-chunk-v1",
        "chunk_rule_version": "deterministic-char-700-v1",
        "real_service_acceptance": False,
        "fixture_count": data["summary"]["fixtures"],
        "successful_fixture_count": data["summary"]["passed"],
        "chunk_count": len(chunks),
        "chunk_report_sha256": hashlib.sha256(canonical).hexdigest(),
        "chunks": chunks,
    }
    path = args.root / "reports" / "chunk-report.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(path), "chunks": len(chunks), "sha256": output["chunk_report_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
