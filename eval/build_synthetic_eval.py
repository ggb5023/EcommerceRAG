#!/usr/bin/env python3
"""Build the first deterministic 60-case synthetic evaluation set."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path


GROUPS = [
    ("own_knowledge", "knowledge", 20),
    ("realtime_business", "factual", 10),
    ("external_t4", "knowledge", 10),
    ("external_t2", "knowledge", 10),
    ("safety_and_routing", "operation", 10),
]


def build(version: str) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    index = 1
    for source, intent, count in GROUPS:
        for offset in range(count):
            tags = [source, "synthetic", "negative" if offset % 5 == 0 else "basic"]
            if offset % 4 == 0:
                tags.append("multi_turn")
            cases.append(
                {
                    "case_id": f"syn-{index:03d}",
                    "query": f"合成案例 {index}：请处理 {source} 场景 {offset + 1}",
                    "expected_doc_ids": [f"syn-doc-{(offset % 4) + 1}"],
                    "expected_answer_points": [f"synthetic-point-{index}"],
                    "intent": intent,
                    "information_source": source,
                    "tags": tags,
                    "authorization": {"tenant_id": "synthetic-tenant", "shop_id": "shop-demo", "role": "operator"},
                    "business_date": "2026-09-29",
                    "source": {"type": "synthetic", "license": "internal-generated", "source_version": version},
                }
            )
            index += 1
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("eval/synthetic_cases.jsonl"))
    parser.add_argument("--version", default="synthetic-m2-v1")
    args = parser.parse_args()
    cases = build(args.version)
    payload = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases).encode()
    digest = hashlib.sha256(payload).hexdigest()
    args.output.write_bytes(payload)
    metadata = {
        "eval_set_version": args.version,
        "case_count": len(cases),
        "source_type": "synthetic",
        "license": "internal-generated",
        "generated_at": date.today().isoformat(),
        "sha256": digest,
        "pipeline_version": "m2-eval-scaffold-v1",
        "model_version": "unset",
        "real_service_acceptance": False,
    }
    args.output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
