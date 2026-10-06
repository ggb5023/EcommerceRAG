#!/usr/bin/env python3
"""Validate provider configuration without contacting a model by default.

The live protocol is intentionally opt-in. This command never prints secret
values and does not persist requests or provider responses.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
if str(PYTHON) not in sys.path:
    sys.path.insert(0, str(PYTHON))

from app.providers import build_provider
from app.providers.config import (
    ProviderConfigError,
    ensure_secure_config_file,
    load_provider_config,
)
from app.providers.contracts import ProviderError

SLOTS = {
    "control": ("CONTROL_MODEL",),
    "embedding": ("EMBEDDING_MODEL",),
    "rerank": ("RERANK_MODEL",),
    "generation": ("GENERATION_MODEL",),
}


def _identifier_fingerprint(value: object) -> str | None:
    """Return a non-reversible identifier for an operator report."""
    if not isinstance(value, str) or not value.strip():
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _live_result_metadata(slot: str, result: object) -> dict[str, object]:
    """Extract redacted metadata from a provider result.

    Embedding returns one result per input while the other slots return one
    result.  Keeping that distinction here prevents a successful embedding
    request from being reported without its request ID or usage metadata.
    """
    if slot == "embedding":
        if not isinstance(result, (list, tuple)) or not result:
            return {
                "model_fingerprint": None,
                "request_id_fingerprint": None,
                "request_id_present": False,
                "usage_present": False,
                "metadata_complete": False,
                "error_code": "invalid_result_shape",
            }
        items = list(result)
    else:
        items = [result]

    request_ids = [getattr(item, "request_id", "") for item in items]
    models = [getattr(item, "model", "") for item in items]
    usages = [getattr(item, "usage", None) for item in items]
    request_id_present = all(isinstance(value, str) and bool(value.strip()) for value in request_ids)
    usage_present = all(value is not None for value in usages)
    model_fingerprint = _identifier_fingerprint(models[0]) if models else None
    request_id_fingerprint = (
        _identifier_fingerprint(request_ids[0]) if request_ids else None
    )
    consistent_model = bool(models) and all(value == models[0] for value in models)
    consistent_request_id = bool(request_ids) and all(value == request_ids[0] for value in request_ids)
    metadata_complete = request_id_present and usage_present and consistent_model and consistent_request_id
    error_code = None
    if not request_id_present:
        error_code = "missing_request_id"
    elif not usage_present:
        error_code = "missing_usage"
    elif not consistent_model or not consistent_request_id:
        error_code = "inconsistent_metadata"
    return {
        "model_fingerprint": model_fingerprint,
        "request_id_fingerprint": request_id_fingerprint,
        "request_id_present": request_id_present,
        "usage_present": usage_present,
        "metadata_complete": metadata_complete,
        "error_code": error_code,
    }


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _usage_summary(value: object) -> dict[str, object]:
    if value is None:
        return {"status": "unknown"}
    if not isinstance(value, dict):
        raise TypeError("usage must be an object or null")
    tokens = value.get("tokens")
    if tokens is not None and (not isinstance(tokens, int) or tokens < 0):
        raise ValueError("usage.tokens must be a non-negative integer")
    return {
        "status": "present",
        "token_fields": sorted(k for k in value if "token" in k),
    }


def _write_live_report(
    output_path: pathlib.Path,
    config_path: pathlib.Path,
    config: object,
    reports: list[dict[str, object]],
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Atomically persist only redacted metadata from a live smoke.

    The provider config object contains the API key, so this function selects
    fields explicitly instead of serializing it.  A temporary file in the
    destination directory prevents readers from observing a partial report.
    """
    endpoint = getattr(config, "endpoint", "")
    endpoint_host = urlsplit(endpoint).hostname if isinstance(endpoint, str) else None
    statuses = [item.get("status") for item in reports]
    report = {
        "report_version": "provider-smoke-v3",
        "generated_at_utc": generated_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "profile": getattr(config, "profile", None),
        "endpoint_host": endpoint_host,
        "region": getattr(config, "region", None),
        "embedding_dimensions": getattr(config, "embedding_dimensions", None),
        "config_file_permissions": oct(config_path.stat().st_mode & 0o777)[2:].zfill(3),
        "online_requests_made": True,
        "raw_payload_saved": False,
        "real_service_acceptance": False,
        "m1_connected": False,
        "overall_status": "PASS" if reports and all(status == "PASS" for status in statuses) else "FAIL",
        "slots": reports,
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return report


def validate_response_fixture(path: pathlib.Path) -> dict[str, object]:
    """Validate redacted provider smoke responses without retaining payloads."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("fixture must be an object")
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ValueError("responses must be a non-empty list")
    allowed_slots = set(SLOTS)
    seen: set[str] = set()
    summary: list[dict[str, object]] = []
    for response in responses:
        if not isinstance(response, dict):
            raise TypeError("each response must be an object")
        slot = _require_string(response.get("slot"), "slot")
        if slot not in allowed_slots or slot in seen:
            raise ValueError(f"invalid or duplicate slot: {slot}")
        seen.add(slot)
        request_id = _require_string(
            response.get("provider_request_id"), "provider_request_id"
        )
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
            if response.get("dense_values") is not None:
                dense_values = response["dense_values"]
                if not isinstance(dense_values, int) or dense_values != 1024:
                    raise ValueError("embedding dense_values must be 1024")
            if output == "dense&sparse" and response.get("sparse_items") is not None:
                sparse_items = response["sparse_items"]
                if not isinstance(sparse_items, int) or sparse_items <= 0:
                    raise ValueError("embedding sparse_items must be positive")
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
            if not all(
                isinstance(score, (int, float)) and 0 <= score <= 1 for score in scores
            ):
                raise ValueError("rerank scores must be between 0 and 1")
        if slot in {"control", "generation"} and status == 200:
            if not isinstance(response.get("structured"), dict):
                raise ValueError(f"{slot}.structured must be an object")
            finish_reason = response.get("finish_reason")
            if finish_reason not in {
                "stop",
                "length",
                "tool_calls",
                "content_filter",
                "end_turn",
                "eos",
                "completed",
            }:
                raise ValueError(f"{slot}.finish_reason is invalid")
        summary.append(
            {
                "slot": slot,
                "status": status,
                "provider_request_id_present": bool(request_id),
                "latency_ms": latency_ms,
                "usage": usage,
            }
        )
    missing_slots = sorted(allowed_slots - seen)
    if missing_slots:
        raise ValueError("missing slots: " + ",".join(missing_slots))
    return {"slots": summary, "raw_payload_saved": False}


async def _live_smoke(
    config_path: pathlib.Path, output_path: pathlib.Path | None = None
) -> tuple[str, int]:
    try:
        ensure_secure_config_file(config_path)
        config = load_provider_config(config_path)
    except ProviderConfigError as exc:
        print(f"NOT_RUN/CONFIG_BLOCKED {exc}")
        return "CONFIG_BLOCKED", 3
    if config.profile == "mock":
        print("NOT_RUN profile=mock deterministic mock remains active")
        return "MOCK", 0
    provider = build_provider(config)
    checks = [
        (
            "control",
            lambda: provider.control(
                [
                    {
                        "role": "user",
                        "content": "Return a JSON object with intent=knowledge.",
                    }
                ],
                response_schema={"name": "smoke", "schema": {"type": "object"}},
            ),
        ),
        (
            "embedding",
            lambda: provider.embed(
                ["provider smoke"],
                text_type="query",
                dimensions=1024,
                output_type="dense&sparse",
            ),
        ),
        (
            "rerank",
            lambda: provider.rerank(
                "provider smoke",
                ["provider smoke candidate", "unrelated candidate"],
                top_n=2,
            ),
        ),
        (
            "generation",
            lambda: provider.generate(
                [{"role": "user", "content": "Reply with the word OK."}]
            ),
        ),
    ]
    reports: list[dict[str, object]] = []
    for slot, call in checks:
        started = time.monotonic()
        try:
            result = await call()
            metadata = _live_result_metadata(slot, result)
            metadata_ok = bool(metadata["metadata_complete"])
            reports.append(
                {
                    "slot": slot,
                    "status": "PASS" if metadata_ok else "FAIL",
                    "error_code": metadata["error_code"],
                    "model_fingerprint": metadata["model_fingerprint"],
                    "request_id_fingerprint": metadata["request_id_fingerprint"],
                    "request_id_present": metadata["request_id_present"],
                    "usage_present": metadata["usage_present"],
                    "metadata_complete": metadata_ok,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        except ProviderError as exc:
            reports.append(
                {
                    "slot": slot,
                    "status": "FAIL",
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        except Exception:  # noqa: BLE001 - redact all unexpected vendor errors
            # Do not print exception text: third-party exceptions can contain
            # request bodies, URLs or authorization material.
            reports.append(
                {
                    "slot": slot,
                    "status": "FAIL",
                    "error_code": "unexpected_error",
                    "retryable": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
    passed = all(report["status"] == "PASS" for report in reports)
    persisted_report = None
    if output_path is not None:
        try:
            persisted_report = _write_live_report(output_path, config_path, config, reports)
        except (OSError, TypeError, ValueError) as exc:
            print(f"REPORT_WRITE_FAIL {type(exc).__name__}")
            return "REPORT_WRITE_FAIL", 5
    print(
        ("SMOKE_PASS" if passed else "SMOKE_FAIL")
        + " "
        + json.dumps(
            {
                "profile": config.profile,
                "slots": reports,
                "report_path": str(output_path) if persisted_report is not None else None,
                "raw_payload_saved": False,
            },
            sort_keys=True,
        )
    )
    return ("PASS" if passed else "FAIL"), (0 if passed else 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/ecommerce-rag/providers.env")
    parser.add_argument(
        "--live", action="store_true", help="reserved for an explicit vendor adapter"
    )
    parser.add_argument(
        "--response-fixture",
        type=pathlib.Path,
        help="validate a redacted offline response fixture; never contacts a provider",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        help="write an atomic metadata-only report for a live smoke",
    )
    args = parser.parse_args()
    if args.response_fixture:
        try:
            report = validate_response_fixture(args.response_fixture)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"FIXTURE_FAIL {exc}")
            return 2
        print("FIXTURE_PASS " + json.dumps(report, ensure_ascii=False, sort_keys=True))
        print("LIVE_NOT_RUN no network request was made")
        return 0
    if args.live:
        _, exit_code = asyncio.run(_live_smoke(pathlib.Path(args.env), args.output))
        return exit_code
    try:
        ensure_secure_config_file(pathlib.Path(args.env))
        config = load_provider_config(pathlib.Path(args.env))
    except ProviderConfigError as exc:
        print(f"{exc.code} {exc}")
        return 3 if exc.code == "CONFIG_BLOCKED" else 2
    print(
        "CONFIG_PASS profile="
        + config.profile
        + " provider slots, endpoint, quota and embedding dimension validated"
    )
    print("LIVE_NOT_RUN no network request was made")
    return 0


if __name__ == "__main__":
    sys.exit(main())
