#!/usr/bin/env python3
"""Run the approved context-v2 cases in bounded Bailian batches.

This runner is deliberately separate from the single-window evaluator.  It
uses the evaluator's input, authorization, date, evidence and redaction
contracts, while adding a deterministic case plan, request budget, rate gate,
checkpoint/resume, and fail-stop behavior.  A cost unit is explicitly one
provider request equivalent; it is not a currency estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = Path(__file__).resolve().parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

import run_bailian_retrieval as retrieval
from answer_point_evidence import validate_review
from app.providers import build_provider
from app.providers.config import (
    ProviderConfigError,
    ensure_secure_config_file,
    load_provider_config,
)
from app.providers.contracts import ProviderError

DEFAULT_CONTEXT_REVIEW = Path(
    "/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/"
    "answer-point-evidence-review-revision-1-context-v2.json"
)
DEFAULT_REVISION_ROOT = Path(
    "/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional"
)
DEFAULT_OUTPUT = Path(
    "/var/lib/ecommerce-rag/real-docs/reports/"
    "bailian-retrieval-revision-1-context-v2-batches.json"
)
DEFAULT_CHECKPOINT = Path(
    "/var/lib/ecommerce-rag/real-docs/reports/"
    "bailian-retrieval-revision-1-context-v2-batches.checkpoint.json"
)

SAFE_FAILURE_CODES = {
    "authentication", "cancelled", "config_blocked", "http_error",
    "invalid_input", "invalid_request", "invalid_response", "rate_limited",
    "schema_error", "timeout", "upstream_error", "vendor_error",
    "no_eligible_chunks", "no_candidates", "no_rerank_evidence", "batch_failed",
}


class BatchPlanError(ValueError):
    """A deterministic plan or budget cannot be trusted."""


class RequestBudgetExceeded(RuntimeError):
    """The configured request or cost-equivalent budget was exhausted."""


class RequestBudget:
    """Count requests and gate them by total and rolling minute limits."""

    def __init__(
        self,
        *,
        max_requests: int,
        max_requests_per_minute: int,
        max_cost_units: int,
        initial_requests: int = 0,
        initial_cost_units: int = 0,
        initial_recent_timestamps: Sequence[float] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if min(max_requests, max_requests_per_minute, max_cost_units) <= 0:
            raise BatchPlanError("request and cost limits must be positive")
        if min(initial_requests, initial_cost_units) < 0:
            raise BatchPlanError("initial request and cost counts must be non-negative")
        if initial_requests > max_requests or initial_cost_units > max_cost_units:
            raise BatchPlanError("checkpoint budget already exceeded")
        self.max_requests = max_requests
        self.max_requests_per_minute = max_requests_per_minute
        self.max_cost_units = max_cost_units
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.total_requests = initial_requests
        self.cost_units = initial_cost_units
        self.timestamps: collections.deque[float] = collections.deque()
        self.wall_timestamps: collections.deque[float] = collections.deque()
        now = self.clock()
        wall_now = self.wall_clock()
        for timestamp in initial_recent_timestamps:
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(timestamp):
                raise BatchPlanError("initial rate timestamps must be finite numbers")
            age = max(0.0, wall_now - float(timestamp))
            if age <= 60.0:
                self.timestamps.append(now - age)
                self.wall_timestamps.append(float(timestamp))

    def recent_wall_timestamps(self) -> list[float]:
        """Return the current rolling-window timestamps for checkpointing."""
        return list(self.wall_timestamps)

    async def acquire(self, operation: str) -> None:
        """Wait for the rolling RPM gate, then reserve one request unit."""
        # Reject a spent total budget before waiting on the rolling window.
        # This keeps a failed run fail-stop and avoids an unnecessary minute
        # sleep after the caller is already unable to make another request.
        if self.total_requests + 1 > self.max_requests:
            raise RequestBudgetExceeded(f"request_budget_exceeded:{operation}")
        if self.cost_units + 1 > self.max_cost_units:
            raise RequestBudgetExceeded(f"cost_budget_exceeded:{operation}")
        while True:
            now = self.clock()
            while self.timestamps and self.timestamps[0] <= now - 60.0:
                self.timestamps.popleft()
                self.wall_timestamps.popleft()
            if len(self.timestamps) < self.max_requests_per_minute:
                break
            delay = max(0.0, self.timestamps[0] + 60.0 - now)
            await self.sleep(delay)
        now = self.clock()
        self.timestamps.append(now)
        self.wall_timestamps.append(self.wall_clock())
        self.total_requests += 1
        self.cost_units += 1


class BudgetedProvider:
    """Delegate all provider operations through the shared request budget."""

    def __init__(
        self,
        provider: Any,
        budget: RequestBudget,
        on_request_reserved: Callable[[], None] | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget
        self.on_request_reserved = on_request_reserved

    async def _reserve(self, operation: str) -> None:
        await self.budget.acquire(operation)
        if self.on_request_reserved is not None:
            self.on_request_reserved()

    async def embed(self, texts: Sequence[str], **kwargs: Any) -> Any:
        await self._reserve("embedding")
        return await self.provider.embed(texts, **kwargs)

    async def rerank(self, query: str, candidates: Sequence[str], **kwargs: Any) -> Any:
        await self._reserve("rerank")
        return await self.provider.rerank(query, candidates, **kwargs)

    async def generate(self, messages: Sequence[Mapping[str, str]], **kwargs: Any) -> Any:
        await self._reserve("generation")
        return await self.provider.generate(messages, **kwargs)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _identity(
    cases_path: Path,
    corpus_path: Path,
    alignment_path: Path,
    review_path: Path,
    *,
    batch_size: int,
    document_batch_size: int,
) -> dict[str, Any]:
    return {
        "cases_sha256": _sha256(cases_path),
        "corpus_sha256": _sha256(corpus_path),
        "alignment_sha256": _sha256(alignment_path),
        "answer_evidence_review_sha256": _sha256(review_path),
        "batch_size": batch_size,
        "document_batch_size": document_batch_size,
        "pipeline": "bailian-retrieval-v3",
    }


def _batch_cases(cases: Sequence[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    if batch_size <= 0:
        raise BatchPlanError("batch_size must be positive")
    ordered = sorted(cases, key=lambda row: str(row.get("case_id", "")))
    return [ordered[start : start + batch_size] for start in range(0, len(ordered), batch_size)]


def _estimated_requests(
    batches: Sequence[Sequence[dict[str, Any]]],
    corpus: list[dict[str, Any]],
    *,
    document_batch_size: int,
) -> tuple[int, list[int]]:
    per_batch: list[int] = []
    for batch in batches:
        unique_ids = {
            chunk["chunk_id"]
            for case in batch
            for chunk in retrieval._eligible_chunks(case, corpus)
        }
        document_calls = (len(unique_ids) + document_batch_size - 1) // document_batch_size
        per_batch.append(document_calls + 3 * len(batch))
    return sum(per_batch), per_batch


def build_plan(
    cases: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    alignment: Mapping[str, Any],
    support_chunks: Mapping[str, Sequence[Sequence[str]]],
    *,
    batch_size: int,
    document_batch_size: int,
) -> dict[str, Any]:
    audit = retrieval.audit_generation_context(cases, corpus, support_chunks)
    rows = {row["case_id"]: row for row in audit["case_results"]}
    eligible = [case for case in cases if rows.get(case.get("case_id"), {}).get("status") == "WITHIN_BUDGET"]
    excluded = [case for case in cases if case not in eligible]
    batches = _batch_cases(eligible, batch_size)
    estimated_total, estimated_per_batch = _estimated_requests(
        batches, corpus, document_batch_size=document_batch_size
    )
    return {
        "audit": audit,
        "eligible_cases": eligible,
        "excluded_cases": excluded,
        "batches": batches,
        "estimated_requests": estimated_total,
        "estimated_requests_per_batch": estimated_per_batch,
        "excluded_status_counts": {
            status: sum(rows.get(str(case.get("case_id")), {}).get("status") == status for case in excluded)
            for status in ("BUDGET_EXCEEDED", "INELIGIBLE_REQUIRED_CHUNKS", "UNREVIEWED")
        },
    }


def _checkpoint_payload(identity: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_version": "bailian-batch-checkpoint-v1",
        "identity": dict(identity),
        "status": "RUNNING",
        "eligible_case_count": len(plan["eligible_cases"]),
        "excluded_case_count": len(plan["excluded_cases"]),
        "completed_batches": [],
        "active_batch": None,
        "failed_batch": None,
        "request_count": 0,
        "cost_units": 0,
        "recent_request_timestamps": [],
        "real_service_acceptance": False,
        "m1_connected": False,
    }


def _batch_payload_sha256(record: Mapping[str, Any]) -> str:
    """Hash the immutable part of a completed batch checkpoint record."""
    payload = {
        key: record.get(key)
        for key in (
            "batch_id",
            "case_ids",
            "status",
            "request_count",
            "cost_units",
            "slot_reports",
            "case_results",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_batch_record(
    record: Mapping[str, Any],
    expected_case_ids: Sequence[str],
    *,
    require_payload_hash: bool,
) -> None:
    """Validate a completed batch payload before it can be resumed or skipped."""
    if record.get("status") != "PASS":
        raise BatchPlanError("checkpoint_completed_batch_status_invalid")
    if record.get("case_ids") != list(expected_case_ids):
        raise BatchPlanError("checkpoint_case_ids_mismatch")
    case_results = record.get("case_results")
    slot_reports = record.get("slot_reports")
    if not isinstance(case_results, list) or not isinstance(slot_reports, list):
        raise BatchPlanError("checkpoint_batch_payload_invalid")
    if any(not isinstance(item, Mapping) for item in case_results):
        raise BatchPlanError("checkpoint_case_results_invalid")
    if any(not isinstance(item, Mapping) for item in slot_reports):
        raise BatchPlanError("checkpoint_slot_reports_invalid")
    result_case_ids = [item.get("case_id") for item in case_results]
    if result_case_ids != list(expected_case_ids):
        raise BatchPlanError("checkpoint_case_results_case_ids_mismatch")
    payload_hash = record.get("payload_sha256")
    if require_payload_hash:
        if (
            not isinstance(payload_hash, str)
            or len(payload_hash) != 64
            or payload_hash.lower() != payload_hash
            or any(char not in "0123456789abcdef" for char in payload_hash)
        ):
            raise BatchPlanError("checkpoint_batch_payload_hash_invalid")
        try:
            expected_hash = _batch_payload_sha256(record)
        except (TypeError, ValueError, OverflowError) as error:
            raise BatchPlanError("checkpoint_batch_payload_invalid") from error
        if payload_hash != expected_hash:
            raise BatchPlanError("checkpoint_batch_payload_hash_mismatch")


def _load_checkpoint(path: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    try:
        checkpoint = _load_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BatchPlanError(f"checkpoint_invalid:{type(error).__name__}") from error
    if not isinstance(checkpoint, Mapping) or checkpoint.get("checkpoint_version") != "bailian-batch-checkpoint-v1":
        raise BatchPlanError("checkpoint_version_invalid")
    if checkpoint.get("identity") != dict(identity):
        raise BatchPlanError("checkpoint_identity_mismatch")
    if checkpoint.get("real_service_acceptance") is not False or checkpoint.get("m1_connected") is not False:
        raise BatchPlanError("checkpoint_boundary_invalid")
    completed = checkpoint.get("completed_batches", [])
    if not isinstance(completed, list) or any(not isinstance(row, Mapping) for row in completed):
        raise BatchPlanError("checkpoint_completed_batches_invalid")
    for field in ("request_count", "cost_units"):
        value = checkpoint.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BatchPlanError(f"checkpoint_{field}_invalid")
    timestamps = checkpoint.get("recent_request_timestamps", [])
    if (
        not isinstance(timestamps, list)
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in timestamps
        )
    ):
        raise BatchPlanError("checkpoint_recent_request_timestamps_invalid")
    return dict(checkpoint)


def _validate_checkpoint_batches(checkpoint: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    """Reject checkpoints that could skip or invent deterministic batches."""
    completed = checkpoint.get("completed_batches", [])
    batches = plan.get("batches", [])
    status = checkpoint.get("status")
    if status not in {"RUNNING", "FAILED", "COMPLETE"}:
        raise BatchPlanError("checkpoint_status_invalid")
    if checkpoint.get("eligible_case_count") != len(plan.get("eligible_cases", [])):
        raise BatchPlanError("checkpoint_eligible_case_count_mismatch")
    if checkpoint.get("excluded_case_count") != len(plan.get("excluded_cases", [])):
        raise BatchPlanError("checkpoint_excluded_case_count_mismatch")
    if len(completed) > len(batches):
        raise BatchPlanError("checkpoint_completed_batch_count_invalid")
    previous_requests = 0
    previous_cost = 0
    for index, record in enumerate(completed):
        if not isinstance(record, Mapping):
            raise BatchPlanError("checkpoint_completed_batch_invalid")
        expected = batches[index]
        expected_id = f"batch-{index + 1:03d}"
        if record.get("batch_id") != expected_id:
            raise BatchPlanError("checkpoint_completed_batches_not_prefix")
        expected_case_ids = [str(case["case_id"]) for case in expected]
        _validate_batch_record(record, expected_case_ids, require_payload_hash=True)
        request_count = record.get("request_count", 0)
        cost_units = record.get("cost_units", request_count)
        if (
            not isinstance(request_count, int)
            or isinstance(request_count, bool)
            or request_count < previous_requests
            or not isinstance(cost_units, int)
            or isinstance(cost_units, bool)
            or cost_units < previous_cost
            or cost_units != request_count
        ):
            raise BatchPlanError("checkpoint_batch_budget_invalid")
        previous_requests = request_count
        previous_cost = cost_units
    request_count = int(checkpoint.get("request_count", 0))
    cost_units = int(checkpoint.get("cost_units", 0))
    if request_count < previous_requests:
        raise BatchPlanError("checkpoint_request_count_mismatch")
    if cost_units < previous_cost or cost_units != request_count:
        raise BatchPlanError("checkpoint_cost_units_mismatch")
    failed_batch = checkpoint.get("failed_batch")
    active_batch = checkpoint.get("active_batch")
    if status == "FAILED":
        if not isinstance(failed_batch, Mapping):
            raise BatchPlanError("checkpoint_failed_batch_missing")
        if len(completed) >= len(batches):
            raise BatchPlanError("checkpoint_failed_batch_index_invalid")
        expected_failed = batches[len(completed)]
        expected_failed_id = f"batch-{len(completed) + 1:03d}"
        expected_failed_case_ids = [str(case["case_id"]) for case in expected_failed]
        if failed_batch.get("batch_id") != expected_failed_id:
            raise BatchPlanError("checkpoint_failed_batch_id_mismatch")
        if failed_batch.get("case_ids") != expected_failed_case_ids:
            raise BatchPlanError("checkpoint_failed_batch_case_ids_mismatch")
        if not isinstance(failed_batch.get("error_code"), str) or not failed_batch["error_code"]:
            raise BatchPlanError("checkpoint_failed_batch_error_invalid")
        failed_requests = failed_batch.get("reserved_request_count")
        if (
            not isinstance(failed_requests, int)
            or isinstance(failed_requests, bool)
            or failed_requests < 0
            or request_count != previous_requests + failed_requests
        ):
            raise BatchPlanError("checkpoint_failed_batch_request_count_invalid")
        if active_batch is not None:
            raise BatchPlanError("checkpoint_active_batch_unexpected")
    elif failed_batch is not None:
        raise BatchPlanError("checkpoint_failed_batch_unexpected")
    if status == "RUNNING" and active_batch is not None:
        if not isinstance(active_batch, Mapping) or len(completed) >= len(batches):
            raise BatchPlanError("checkpoint_active_batch_invalid")
        expected_active = batches[len(completed)]
        expected_active_id = f"batch-{len(completed) + 1:03d}"
        expected_active_case_ids = [str(case["case_id"]) for case in expected_active]
        if active_batch.get("batch_id") != expected_active_id:
            raise BatchPlanError("checkpoint_active_batch_id_mismatch")
        if active_batch.get("case_ids") != expected_active_case_ids:
            raise BatchPlanError("checkpoint_active_batch_case_ids_mismatch")
        if not isinstance(active_batch.get("reserved_request_count"), int) or isinstance(active_batch.get("reserved_request_count"), bool) or active_batch["reserved_request_count"] < 0:
            raise BatchPlanError("checkpoint_active_batch_request_count_invalid")
        if request_count != previous_requests + active_batch["reserved_request_count"]:
            raise BatchPlanError("checkpoint_active_batch_budget_mismatch")
    elif status != "RUNNING" and active_batch is not None:
        raise BatchPlanError("checkpoint_active_batch_unexpected")
    elif status == "RUNNING" and request_count != previous_requests:
        raise BatchPlanError("checkpoint_running_budget_mismatch")
    if status == "COMPLETE" and request_count != previous_requests:
        raise BatchPlanError("checkpoint_complete_budget_mismatch")
    if status == "COMPLETE" and len(completed) != len(batches):
        raise BatchPlanError("checkpoint_complete_batch_count_invalid")
    if status == "RUNNING" and len(completed) == len(batches):
        raise BatchPlanError("checkpoint_running_batch_count_invalid")


def _record_budget(checkpoint: dict[str, Any], budget: RequestBudget) -> None:
    """Persist every consumed request, including requests before a failure."""
    checkpoint["request_count"] = budget.total_requests
    checkpoint["cost_units"] = budget.cost_units
    checkpoint["recent_request_timestamps"] = budget.recent_wall_timestamps()


def _failure_diagnostics(run: Mapping[str, Any], case_ids: Sequence[str]) -> dict[str, Any]:
    """Retain only case identity and known failure codes, never exception text."""
    rows = []
    seen: set[str] = set()
    for row in run.get("case_results", []):
        case_id = row.get("case_id")
        if case_id not in case_ids or case_id in seen:
            raise BatchPlanError("failed_case_identity_invalid")
        seen.add(case_id)
        item = {"case_id": case_id, "status": "FAIL" if row.get("status") == "FAIL" else "COMPLETED"}
        if item["status"] == "FAIL":
            code = row.get("error_code")
            item["error_code"] = code if isinstance(code, str) and code in SAFE_FAILURE_CODES else "unclassified_failure"
        rows.append(item)
    issues = []
    for issue in run.get("issues", []):
        parts = str(issue).split(":", 1)
        code = parts[-1]
        if code not in SAFE_FAILURE_CODES:
            issues.append("unclassified_failure")
        elif len(parts) == 2 and parts[0] in {"embedding", "rerank", "generation"}:
            issues.append(":".join(parts))
        else:
            issues.append(code)
    return {"case_statuses": rows, "issues": sorted(set(issues))}


def report_checkpoint(
    checkpoint: Mapping[str, Any], plan: Mapping[str, Any],
    identity: Mapping[str, Any], limits: Mapping[str, int],
) -> dict[str, Any]:
    """Export verified checkpoint progress offline; never infer missing results."""
    _validate_checkpoint_batches(checkpoint, plan)
    completed = checkpoint["completed_batches"]
    failed = checkpoint.get("failed_batch")
    results = [row for batch in completed for row in batch["case_results"]]
    slots = [row for batch in completed for row in batch["slot_reports"]]
    run = {
        "status": "PASS" if checkpoint["status"] == "COMPLETE" else checkpoint["status"],
        "case_results": results, "slot_reports": slots,
        "request_count": checkpoint["request_count"], "cost_units": checkpoint["cost_units"],
        "online_requests_made": checkpoint["request_count"] > 0,
    }
    report = make_report(plan, run, identity, limits)
    remaining = sum(plan["estimated_requests_per_batch"][len(completed):])
    required = checkpoint["request_count"] + remaining
    diagnostics = failed.get("diagnostics") if failed is not None else None
    if diagnostics is not None:
        if not isinstance(diagnostics, Mapping):
            raise BatchPlanError("failed_diagnostics_invalid")
        diagnostics = _failure_diagnostics({
            "case_results": diagnostics.get("case_statuses", []),
            "issues": diagnostics.get("issues", []),
        }, failed["case_ids"])
    report.update({
        "report_origin": "verified_checkpoint_offline_export",
        "new_provider_requests": 0,
        "request_count_definition": "reserved request equivalents, not confirmed responses or currency",
        "completed_batch_count": len(completed),
        "completed_case_count": len(results),
        "unmeasured_case_count": len(plan["eligible_cases"]) - len(results),
        "remaining_estimated_request_count": remaining,
        "minimum_total_request_limit_for_resume": required,
        "resume_within_current_limits": required <= min(limits["max_requests"], limits["max_cost_units"]),
        "failed_batch": None if failed is None else {
            "batch_id": failed["batch_id"], "case_ids": failed["case_ids"],
            "error_code": failed["error_code"] if failed["error_code"] in SAFE_FAILURE_CODES else "unclassified_failure",
            "reserved_request_count": failed["reserved_request_count"],
            "diagnostics": diagnostics,
            "failure_detail_status": "AVAILABLE" if failed.get("diagnostics") is not None else "UNAVAILABLE_LEGACY_CHECKPOINT",
        },
    })
    return report


async def run_batches(
    provider: Any,
    plan: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    max_requests: int,
    max_requests_per_minute: int,
    max_cost_units: int,
    document_batch_size: int,
    checkpoint_path: Path | None = None,
    resume: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
    wall_clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    estimated = int(plan["estimated_requests"])
    if estimated > max_requests or estimated > max_cost_units:
        raise BatchPlanError("estimated_budget_exceeded")
    if checkpoint_path and checkpoint_path.exists():
        if not resume:
            raise BatchPlanError("checkpoint_exists_use_resume")
        checkpoint = _load_checkpoint(checkpoint_path, identity)
    else:
        checkpoint = _checkpoint_payload(identity, plan)
    _validate_checkpoint_batches(checkpoint, plan)
    completed = {str(row.get("batch_id")): row for row in checkpoint.get("completed_batches", [])}
    budget = RequestBudget(
        max_requests=max_requests,
        max_requests_per_minute=max_requests_per_minute,
        max_cost_units=max_cost_units,
        initial_requests=int(checkpoint.get("request_count", 0)),
        initial_cost_units=int(checkpoint.get("cost_units", 0)),
        initial_recent_timestamps=checkpoint.get("recent_request_timestamps", []),
        clock=clock,
        sleep=sleep,
        wall_clock=wall_clock,
    )
    results: list[dict[str, Any]] = []
    slot_reports: list[dict[str, Any]] = []
    batches = plan["batches"]
    for index, batch in enumerate(batches):
        batch_id = f"batch-{index + 1:03d}"
        case_ids = [str(case["case_id"]) for case in batch]
        if batch_id in completed:
            results.extend(completed[batch_id].get("case_results", []))
            slot_reports.extend(completed[batch_id].get("slot_reports", []))
            continue
        previous_active = checkpoint.get("active_batch")
        previous_failed = checkpoint.get("failed_batch")
        reserved_request_count = 0
        if isinstance(previous_active, Mapping) and previous_active.get("batch_id") == batch_id:
            reserved_request_count = int(previous_active["reserved_request_count"])
        elif isinstance(previous_failed, Mapping) and previous_failed.get("batch_id") == batch_id:
            reserved_request_count = int(previous_failed["reserved_request_count"])
        checkpoint["status"] = "RUNNING"
        checkpoint["failed_batch"] = None
        checkpoint["active_batch"] = {
            "batch_id": batch_id,
            "case_ids": case_ids,
            "reserved_request_count": reserved_request_count,
        }
        if checkpoint_path:
            # Publish the active batch before the first provider call.  A
            # process that dies in the request setup window must still leave
            # an auditable resume point, even when no request was reserved.
            _atomic_json(checkpoint_path, checkpoint, replace=True)

        def persist_reserved_request() -> None:
            checkpoint["active_batch"]["reserved_request_count"] += 1
            _record_budget(checkpoint, budget)
            if checkpoint_path:
                _atomic_json(checkpoint_path, checkpoint, replace=True)

        budgeted_provider = BudgetedProvider(provider, budget, persist_reserved_request)
        try:
            run = await retrieval.run_live(
                budgeted_provider,
                list(batch),
                plan["corpus"],
                plan["alignment"],
                batch_size=document_batch_size,
                answer_evidence=plan["answer_evidence"],
                answer_evidence_chunks=plan["support_chunks"],
                fail_fast=True,
            )
        except (ProviderError, RequestBudgetExceeded, ValueError) as error:
            _record_budget(checkpoint, budget)
            checkpoint["status"] = "FAILED"
            reserved_request_count = checkpoint["active_batch"]["reserved_request_count"]
            checkpoint["active_batch"] = None
            checkpoint["failed_batch"] = {
                "batch_id": batch_id,
                "case_ids": case_ids,
                "error_code": getattr(error, "code", type(error).__name__),
                "reserved_request_count": reserved_request_count,
            }
            if checkpoint_path:
                _atomic_json(checkpoint_path, checkpoint, replace=True)
            raise
        batch_record = {
            "batch_id": batch_id,
            "case_ids": case_ids,
            "status": run.get("status", "FAIL"),
            "request_count": budget.total_requests,
            "cost_units": budget.cost_units,
            "slot_reports": run.get("slot_reports", []),
            "case_results": run.get("case_results", []),
        }
        if batch_record["status"] != "PASS":
            checkpoint["status"] = "FAILED"
            reserved_request_count = checkpoint["active_batch"]["reserved_request_count"]
            checkpoint["active_batch"] = None
            checkpoint["failed_batch"] = {
                "batch_id": batch_id,
                "case_ids": case_ids,
                "error_code": "batch_failed",
                "reserved_request_count": reserved_request_count,
                "diagnostics": _failure_diagnostics(run, case_ids),
            }
            _record_budget(checkpoint, budget)
            if checkpoint_path:
                _atomic_json(checkpoint_path, checkpoint, replace=True)
            raise BatchPlanError("batch_failed")
        try:
            batch_record["payload_sha256"] = _batch_payload_sha256(batch_record)
            _validate_batch_record(batch_record, case_ids, require_payload_hash=True)
        except (BatchPlanError, TypeError, ValueError, OverflowError) as error:
            _record_budget(checkpoint, budget)
            checkpoint["status"] = "FAILED"
            reserved_request_count = checkpoint["active_batch"]["reserved_request_count"]
            checkpoint["active_batch"] = None
            checkpoint["failed_batch"] = {
                "batch_id": batch_id,
                "case_ids": case_ids,
                "error_code": getattr(error, "code", type(error).__name__),
                "reserved_request_count": reserved_request_count,
            }
            if checkpoint_path:
                _atomic_json(checkpoint_path, checkpoint, replace=True)
            if isinstance(error, BatchPlanError):
                raise
            raise BatchPlanError("batch_payload_invalid") from error
        completed[batch_id] = batch_record
        slot_reports.extend(batch_record["slot_reports"])
        checkpoint["completed_batches"] = list(completed.values())
        checkpoint["active_batch"] = None
        # Persist the final batch and terminal state together: a crash after
        # this write must be resumable without issuing another provider call.
        if len(completed) == len(batches):
            checkpoint["status"] = "COMPLETE"
        _record_budget(checkpoint, budget)
        checkpoint["recent_request_timestamps"] = budget.recent_wall_timestamps()
        if checkpoint_path:
            _atomic_json(checkpoint_path, checkpoint, replace=True)
        results.extend(batch_record["case_results"])
    checkpoint["status"] = "COMPLETE"
    checkpoint["failed_batch"] = None
    if checkpoint_path:
        _atomic_json(checkpoint_path, checkpoint, replace=True)
    return {
        "status": "PASS",
        "case_results": results,
        "request_count": budget.total_requests,
        "cost_units": budget.cost_units,
        "completed_batch_count": len(batches),
        "slot_reports": slot_reports,
        "online_requests_made": budget.total_requests > 0,
    }


def make_report(plan: Mapping[str, Any], run: Mapping[str, Any], identity: Mapping[str, Any], limits: Mapping[str, int]) -> dict[str, Any]:
    return {
        "report_version": "bailian-batch-v1",
        "provider_profile": "aliyun-bailian",
        "identity": dict(identity),
        "batch_size": len(plan["batches"][0]) if plan["batches"] else 0,
        "eligible_case_count": len(plan["eligible_cases"]),
        "excluded_case_count": len(plan["excluded_cases"]),
        "excluded_status_counts": dict(plan["excluded_status_counts"]),
        "estimated_request_count": plan["estimated_requests"],
        "estimated_requests_per_batch": plan["estimated_requests_per_batch"],
        "batch_plan": [
            {
                "batch_id": f"batch-{index + 1:03d}",
                "case_ids": [str(case["case_id"]) for case in batch],
                "estimated_request_count": plan["estimated_requests_per_batch"][index],
            }
            for index, batch in enumerate(plan["batches"])
        ],
        "estimated_cost_units": plan["estimated_requests"],
        "cost_unit_definition": "one provider request equivalent; not currency",
        "limits": dict(limits),
        "actual_request_count": run.get("request_count", 0),
        "actual_cost_units": run.get("cost_units", 0),
        "slot_reports": run.get("slot_reports", []),
        "case_results": run.get("case_results", []),
        "status": run.get("status", "FAIL"),
        "answer_quality_status": "NOT_RUN",
        "model_quality_claim": False,
        "real_service_acceptance": False,
        "m1_connected": False,
        "online_requests_made": bool(run.get("online_requests_made", False)),
    }


async def main_async(args: argparse.Namespace) -> int:
    cases, corpus, alignment, issues = retrieval.load_inputs(
        args.cases, args.corpus, args.alignment, args.metadata
    )
    if issues:
        print(json.dumps({"status": "FAIL", "issues": issues, "real_service_acceptance": False}))
        return 2
    answer_flags, answer_review = validate_review(
        args.answer_evidence,
        args.cases,
        args.corpus,
        args.alignment,
        cases,
        corpus,
        alignment,
    )
    if answer_review.get("issues") or answer_review.get("status") != "APPROVED":
        print(json.dumps({"status": "FAIL", "issues": answer_review.get("issues", ["answer_evidence_not_approved"])}))
        return 2
    support_chunks = answer_review["approved_support_chunk_ids"]
    plan = build_plan(
        cases,
        corpus,
        alignment,
        support_chunks,
        batch_size=args.batch_size,
        document_batch_size=args.document_batch_size,
    )
    plan = {**plan, "corpus": corpus, "alignment": alignment, "answer_evidence": answer_flags, "support_chunks": support_chunks}
    if len(plan["eligible_cases"]) != 53 or len(plan["excluded_cases"]) != 7:
        print(json.dumps({"status": "FAIL", "issues": ["context_v2_case_partition_unexpected"]}))
        return 2
    identity = _identity(
        args.cases, args.corpus, args.alignment, args.answer_evidence,
        batch_size=args.batch_size, document_batch_size=args.document_batch_size,
    )
    if getattr(args, "report_checkpoint", False):
        if args.output.exists():
            print(json.dumps({"status": "FAIL", "issues": ["output_exists"], "new_provider_requests": 0}))
            return 2
        try:
            checkpoint = _load_checkpoint(args.checkpoint, identity)
            report = report_checkpoint(checkpoint, plan, identity, {
                "max_requests": args.max_requests,
                "max_requests_per_minute": args.max_requests_per_minute,
                "max_cost_units": args.max_cost_units,
            })
            retrieval.write_report(args.output, report)
        except (BatchPlanError, ValueError, OSError) as error:
            print(json.dumps({"status": "FAIL", "issues": [type(error).__name__], "new_provider_requests": 0}))
            return 2
        print(json.dumps({key: report[key] for key in (
            "status", "completed_case_count", "unmeasured_case_count", "actual_request_count",
            "remaining_estimated_request_count", "resume_within_current_limits", "new_provider_requests",
        )}))
        return 0
    if args.live and args.output and args.output.exists():
        print(json.dumps({"status": "FAIL", "issues": ["output_exists"], "online_requests_made": False}))
        return 2
    if not args.live:
        print(json.dumps({
            "status": "NOT_RUN",
            "identity": identity,
            "eligible_case_count": 53,
            "excluded_case_count": 7,
            "estimated_request_count": plan["estimated_requests"],
            "estimated_requests_per_batch": plan["estimated_requests_per_batch"],
            "excluded_status_counts": plan["excluded_status_counts"],
            "real_service_acceptance": False,
            "online_requests_made": False,
        }, ensure_ascii=False))
        return 0
    try:
        ensure_secure_config_file(args.env)
        config = load_provider_config(args.env)
        if config.profile != "aliyun-bailian":
            raise ProviderConfigError("provider profile is not aliyun-bailian")
        run = await run_batches(
            build_provider(config), plan, identity=identity,
            max_requests=args.max_requests,
            max_requests_per_minute=args.max_requests_per_minute,
            max_cost_units=args.max_cost_units,
            document_batch_size=args.document_batch_size,
            checkpoint_path=args.checkpoint,
            resume=args.resume,
        )
    except (ProviderConfigError, BatchPlanError, ProviderError, RequestBudgetExceeded, ValueError) as error:
        print(json.dumps({"status": "FAIL", "issues": [getattr(error, "code", type(error).__name__)], "real_service_acceptance": False}))
        return 4
    report = make_report(plan, run, identity, {
        "max_requests": args.max_requests,
        "max_requests_per_minute": args.max_requests_per_minute,
        "max_cost_units": args.max_cost_units,
    })
    if args.output:
        retrieval.write_report(args.output, report)
    print(json.dumps({key: report[key] for key in ("status", "eligible_case_count", "excluded_case_count", "actual_request_count", "real_service_acceptance")}, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--plan", action="store_true", help="validate and print the bounded plan")
    mode.add_argument("--report-checkpoint", action="store_true", help="export checkpoint progress offline without reading Provider configuration")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--document-batch-size", type=int, default=16)
    parser.add_argument("--max-requests", type=int, default=240)
    parser.add_argument("--max-requests-per-minute", type=int, default=60)
    parser.add_argument("--max-cost-units", type=int, default=240)
    parser.add_argument("--env", type=Path, default=Path("/etc/ecommerce-rag/providers.env"))
    parser.add_argument("--cases", type=Path, default=DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.jsonl")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.metadata.json")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_REVISION_ROOT / "documents.jsonl")
    parser.add_argument("--alignment", type=Path, default=DEFAULT_REVISION_ROOT / "alignment-revision-1.json")
    parser.add_argument("--answer-evidence", type=Path, default=DEFAULT_CONTEXT_REVIEW)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
