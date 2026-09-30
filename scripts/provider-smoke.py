#!/usr/bin/env python3
"""Validate provider configuration without contacting a model by default.

The live protocol is intentionally opt-in. This command never prints secret
values and does not persist requests or provider responses.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys


SLOTS = {
    "control": ("CONTROL_MODEL",),
    "embedding": ("EMBEDDING_MODEL",),
    "rerank": ("RERANK_MODEL",),
    "generation": ("GENERATION_MODEL",),
}
REQUIRED = ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL")


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"{path}:{number}: invalid variable name")
        values.setdefault(key, value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument("--live", action="store_true", help="reserved for an explicit vendor adapter")
    args = parser.parse_args()
    try:
        values = load_env(pathlib.Path(args.env))
    except (OSError, ValueError) as exc:
        print(f"CONFIG_FAIL {exc}")
        return 2
    merged = {**values, **os.environ}
    missing = [name for name in REQUIRED if not merged.get(name)]
    missing.extend(name for names in SLOTS.values() for name in names if not merged.get(name))
    dimensions = merged.get("EMBEDDING_DIMENSIONS", "1024")
    if dimensions != "1024":
        print(f"CONFIG_FAIL EMBEDDING_DIMENSIONS must be 1024, got {dimensions}")
        return 2
    if missing:
        print("CONFIG_BLOCKED missing=" + ",".join(sorted(set(missing))))
        return 3
    if args.live:
        print("LIVE_NOT_RUN vendor adapter is not implemented; deterministic mock remains active")
        return 4
    print("CONFIG_PASS provider slots, endpoint, key presence and embedding dimension validated")
    print("LIVE_NOT_RUN no network request was made")
    return 0


if __name__ == "__main__":
    sys.exit(main())
