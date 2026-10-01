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
import json


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


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _usage_summary(value: object) -> dict[str, object]:
    if value is None:
        return {"status": "unknown"}
    if not isinstance(value, dict):
        raise ValueError("usage must be an object or null")
    tokens = value.get("tokens")
    if tokens is not None and (not isinstance(tokens, int) or tokens < 0):
        raise ValueError("usage.tokens must be a non-negative integer")
    return {"status": "present", "token_fields": sorted(k for k in value if "token" in k)}


def validate_response_fixture(path: pathlib.Path) -> dict[str, object]:
    """Validate redacted provider smoke responses without retaining payloads."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture must be an object")
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ValueError("responses must be a non-empty list")
    allowed_slots = set(SLOTS)
    seen: set[str] = set()
    summary: list[dict[str, object]] = []
    for response in responses:
        if not isinstance(response, dict):
            raise ValueError("each response must be an object")
        slot = _require_string(response.get("slot"), "slot")
        if slot not in allowed_slots or slot in seen:
            raise ValueError(f"invalid or duplicate slot: {slot}")
        seen.add(slot)
        request_id = _require_string(response.get("provider_request_id"), "provider_request_id")
        status = response.get("status")
        if not isinstance(status, int) or status < 100 or status > 599:
            raise ValueError(f"{slot}.status must be an HTTP status")
        latency_ms = response.get("latency_ms")
        if not isinstance(latency_ms, (int, float)) or latency_ms < 0:
            raise ValueError(f"{slot}.latency_ms must be non-negative")
        usage = _usage_summary(response.get("usage"))
        if slot == "embedding" and status == 200:
            dimensions = response.get("dimensions")
            if dimensions != 1024:
                raise ValueError("embedding dimensions must be 1024")
            output = response.get("output")
            if output not in {"dense", "dense&sparse"}:
                raise ValueError("embedding output must be dense or dense&sparse")
        if slot == "rerank" and status == 200:
            items = response.get("items")
            if not isinstance(items, list):
                raise ValueError("rerank items must be a list")
            indices = [item.get("index") for item in items if isinstance(item, dict)]
            scores = [item.get("score") for item in items if isinstance(item, dict)]
            if len(indices) != len(items) or len(set(indices)) != len(indices):
                raise ValueError("rerank indices must be unique integers")
            if not all(isinstance(index, int) and index >= 0 for index in indices):
                raise ValueError("rerank indices must be non-negative integers")
            if not all(isinstance(score, (int, float)) and 0 <= score <= 1 for score in scores):
                raise ValueError("rerank scores must be between 0 and 1")
        if slot in {"control", "generation"} and status == 200:
            if not isinstance(response.get("structured"), dict):
                raise ValueError(f"{slot}.structured must be an object")
        seen.add(slot)
        summary.append({"slot": slot, "status": status, "provider_request_id_present": bool(request_id),
                        "latency_ms": latency_ms, "usage": usage})
    missing_slots = sorted(allowed_slots - seen)
    if missing_slots:
        raise ValueError("missing slots: " + ",".join(missing_slots))
    return {"slots": summary, "raw_payload_saved": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument("--live", action="store_true", help="reserved for an explicit vendor adapter")
    parser.add_argument("--response-fixture", type=pathlib.Path,
                        help="validate a redacted offline response fixture; never contacts a provider")
    args = parser.parse_args()
    try:
        values = load_env(pathlib.Path(args.env))
    except (OSError, ValueError) as exc:
        print(f"CONFIG_FAIL {exc}")
        return 2
    if args.response_fixture:
        try:
            report = validate_response_fixture(args.response_fixture)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"FIXTURE_FAIL {exc}")
            return 2
        print("FIXTURE_PASS " + json.dumps(report, ensure_ascii=False, sort_keys=True))
        print("LIVE_NOT_RUN no network request was made")
        return 0
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
