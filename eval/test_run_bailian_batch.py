import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "run_bailian_batch", Path(__file__).with_name("run_bailian_batch.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_default_context_v2_inputs_are_revision_one():
    assert MODULE.DEFAULT_CONTEXT_REVIEW.name.endswith("context-v2.json")
    assert MODULE.DEFAULT_REVISION_ROOT.name == "synthetic-m2-v1-aligned-provisional"


def test_bounded_plan_keeps_seven_ineligible_cases_out_of_provider_calls():
    cases_path = MODULE.DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.jsonl"
    metadata_path = MODULE.DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.metadata.json"
    corpus_path = MODULE.DEFAULT_REVISION_ROOT / "documents.jsonl"
    alignment_path = MODULE.DEFAULT_REVISION_ROOT / "alignment-revision-1.json"
    cases, corpus, alignment, issues = MODULE.retrieval.load_inputs(
        cases_path, corpus_path, alignment_path, metadata_path
    )
    assert issues == []
    _flags, review = MODULE.validate_review(
        MODULE.DEFAULT_CONTEXT_REVIEW,
        cases_path,
        corpus_path,
        alignment_path,
        cases,
        corpus,
        alignment,
    )
    assert review["status"] == "APPROVED"
    plan = MODULE.build_plan(
        cases,
        corpus,
        alignment,
        review["approved_support_chunk_ids"],
        batch_size=5,
        document_batch_size=16,
    )
    assert len(plan["eligible_cases"]) == 53
    assert len(plan["excluded_cases"]) == 7
    assert plan["excluded_status_counts"] == {
        "BUDGET_EXCEEDED": 0,
        "INELIGIBLE_REQUIRED_CHUNKS": 7,
        "UNREVIEWED": 0,
    }
    assert plan["estimated_requests"] > 0
    assert len(plan["batches"]) == 11


def test_request_budget_enforces_total_and_rolling_rate_limits():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()

    async def sleep(delay):
        clock.now += delay

    budget = MODULE.RequestBudget(
        max_requests=3,
        max_requests_per_minute=2,
        max_cost_units=3,
        clock=clock,
        sleep=sleep,
    )

    async def run():
        await budget.acquire("first")
        await budget.acquire("second")
        await budget.acquire("third")
        with pytest.raises(MODULE.RequestBudgetExceeded):
            await budget.acquire("fourth")

    asyncio.run(run())
    assert budget.total_requests == 3
    assert clock.now == 60.0


def _small_plan():
    cases = [{"case_id": "a"}, {"case_id": "b"}]
    return {
        "batches": [cases[:1], cases[1:]],
        "eligible_cases": cases,
        "excluded_cases": [],
        "estimated_requests": 2,
        "estimated_requests_per_batch": [1, 1],
        "excluded_status_counts": {},
        "corpus": [],
        "alignment": {},
        "answer_evidence": {},
        "support_chunks": {},
    }


def _completed_record(batch_id, case_ids, *, request_count=0, cost_units=0):
    record = {
        "batch_id": batch_id,
        "case_ids": list(case_ids),
        "status": "PASS",
        "request_count": request_count,
        "cost_units": cost_units,
        "slot_reports": [],
        "case_results": [{"case_id": case_id, "status": "PASS"} for case_id in case_ids],
    }
    record["payload_sha256"] = MODULE._batch_payload_sha256(record)
    return record


def test_batch_resume_skips_completed_batches_and_preserves_identity(tmp_path, monkeypatch):
    calls = []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        calls.append([case["case_id"] for case in cases])
        return {
            "status": "PASS",
            "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}],
        }

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    identity = {"cases_sha256": "a", "batch_size": 1}
    plan = _small_plan()
    first = asyncio.run(
        MODULE.run_batches(
            object(),
            plan,
            identity=identity,
            max_requests=4,
            max_requests_per_minute=4,
            max_cost_units=4,
            document_batch_size=16,
            checkpoint_path=checkpoint,
        )
    )
    assert first["completed_batch_count"] == 2
    assert calls == [["a"], ["b"]]
    calls.clear()
    second = asyncio.run(
        MODULE.run_batches(
            object(),
            plan,
            identity=identity,
            max_requests=4,
            max_requests_per_minute=4,
            max_cost_units=4,
            document_batch_size=16,
            checkpoint_path=checkpoint,
            resume=True,
        )
    )
    assert second["completed_batch_count"] == 2
    assert calls == []
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data["status"] == "COMPLETE"
    assert checkpoint_data["real_service_acceptance"] is False


def test_batch_failure_stops_and_records_failed_batch(tmp_path, monkeypatch):
    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        return {"status": "FAIL", "case_results": []}

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(MODULE.BatchPlanError, match="batch_failed"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
            )
        )
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["status"] == "FAILED"
    assert data["failed_batch"]["batch_id"] == "batch-001"


def test_provider_failure_checkpoint_preserves_consumed_budget(tmp_path, monkeypatch):
    class Provider:
        async def embed(self, texts):
            return []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        raise MODULE.ProviderError("upstream_error", "fixture failure")

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(MODULE.ProviderError, match="fixture failure"):
        asyncio.run(
            MODULE.run_batches(
                Provider(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
            )
        )
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["status"] == "FAILED"
    assert data["request_count"] == 1
    assert data["cost_units"] == 1
    assert len(data["recent_request_timestamps"]) == 1
    assert data["active_batch"] is None
    assert data["failed_batch"]["reserved_request_count"] == 1


def test_unexpected_interruption_persists_active_batch_budget_and_resume_reuses_it(tmp_path, monkeypatch):
    class Provider:
        async def embed(self, texts):
            raise KeyboardInterrupt()

    async def interrupted_run(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])

    monkeypatch.setattr(MODULE.retrieval, "run_live", interrupted_run)
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            MODULE.run_batches(
                Provider(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
            )
        )
    interrupted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert interrupted["status"] == "RUNNING"
    assert interrupted["request_count"] == 1
    assert interrupted["cost_units"] == 1
    assert interrupted["active_batch"] == {
        "batch_id": "batch-001",
        "case_ids": ["a"],
        "reserved_request_count": 1,
    }

    calls = []

    class ResumingProvider:
        async def embed(self, texts):
            return []

    async def resumed_run(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        calls.append([case["case_id"] for case in cases])
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    monkeypatch.setattr(MODULE.retrieval, "run_live", resumed_run)
    result = asyncio.run(
        MODULE.run_batches(
            ResumingProvider(),
            _small_plan(),
            identity={"cases_sha256": "a", "batch_size": 1},
            max_requests=4,
            max_requests_per_minute=4,
            max_cost_units=4,
            document_batch_size=16,
            checkpoint_path=checkpoint,
            resume=True,
        )
    )
    assert result["status"] == "PASS"
    assert result["request_count"] == 3
    assert calls == [["a"], ["b"]]


def test_resume_rejects_tampered_completed_batch_payload(tmp_path, monkeypatch):
    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    asyncio.run(
        MODULE.run_batches(
            object(),
            _small_plan(),
            identity={"cases_sha256": "a", "batch_size": 1},
            max_requests=4,
            max_requests_per_minute=4,
            max_cost_units=4,
            document_batch_size=16,
            checkpoint_path=checkpoint,
        )
    )
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    data["completed_batches"][0]["case_results"][0]["status"] = "TAMPERED"
    checkpoint.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MODULE.BatchPlanError, match="checkpoint_batch_payload_hash_mismatch"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
                resume=True,
            )
        )


def test_resume_rejects_tampered_failed_batch_identity(tmp_path, monkeypatch):
    async def fail_run(provider, cases, corpus, alignment, **kwargs):
        return {"status": "FAIL", "case_results": []}

    monkeypatch.setattr(MODULE.retrieval, "run_live", fail_run)
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(MODULE.BatchPlanError, match="batch_failed"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
            )
        )
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    data["failed_batch"]["case_ids"] = ["wrong"]
    checkpoint.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MODULE.BatchPlanError, match="checkpoint_failed_batch_case_ids_mismatch"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
                resume=True,
            )
        )


@pytest.mark.parametrize("failed_write", [1, 2])
def test_checkpoint_write_failure_stops_before_provider_call(tmp_path, monkeypatch, failed_write):
    provider_calls = []
    original_write = MODULE._atomic_json
    write_count = 0

    class Provider:
        async def embed(self, texts):
            provider_calls.append(texts)
            return []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["must-not-run"])
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    def fail_checkpoint(*args, **kwargs):
        nonlocal write_count
        write_count += 1
        if write_count == failed_write:
            raise OSError("checkpoint storage unavailable")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    monkeypatch.setattr(MODULE, "_atomic_json", fail_checkpoint)
    with pytest.raises(OSError, match="checkpoint storage unavailable"):
        asyncio.run(
            MODULE.run_batches(
                Provider(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=tmp_path / "checkpoint.json",
            )
        )
    assert provider_calls == []


def test_resume_after_final_batch_write_does_not_repeat_provider_calls(tmp_path, monkeypatch):
    calls = []
    original_write = MODULE._atomic_json

    class Provider:
        async def embed(self, texts):
            return []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        calls.append([case["case_id"] for case in cases])
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    def interrupt_after_final_write(path, payload, **kwargs):
        original_write(path, payload, **kwargs)
        if len(payload["completed_batches"]) == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    monkeypatch.setattr(MODULE, "_atomic_json", interrupt_after_final_write)
    checkpoint = tmp_path / "checkpoint.json"
    run_args = {
        "identity": {"cases_sha256": "a", "batch_size": 1},
        "max_requests": 4,
        "max_requests_per_minute": 4,
        "max_cost_units": 4,
        "document_batch_size": 16,
        "checkpoint_path": checkpoint,
    }
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(MODULE.run_batches(Provider(), _small_plan(), **run_args))
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["status"] == "COMPLETE"
    assert calls == [["a"], ["b"]]
    calls.clear()
    monkeypatch.setattr(MODULE, "_atomic_json", original_write)
    result = asyncio.run(MODULE.run_batches(Provider(), _small_plan(), resume=True, **run_args))
    assert result["status"] == "PASS"
    assert result["request_count"] == 2
    assert len(result["case_results"]) == 2
    assert calls == []


def test_resume_rejects_checkpoint_that_skips_a_batch(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "checkpoint_version": "bailian-batch-checkpoint-v1",
                "identity": {"cases_sha256": "a", "batch_size": 1},
                "status": "RUNNING",
                "eligible_case_count": 2,
                "excluded_case_count": 0,
                "completed_batches": [
                    {"batch_id": "batch-002", "case_ids": ["b"], "status": "PASS", "case_results": [], "slot_reports": []}
                ],
                "failed_batch": None,
                "request_count": 0,
                "cost_units": 0,
                "recent_request_timestamps": [],
                "real_service_acceptance": False,
                "m1_connected": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MODULE.BatchPlanError, match="checkpoint_completed_batches_not_prefix"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=4,
                max_requests_per_minute=4,
                max_cost_units=4,
                document_batch_size=16,
                checkpoint_path=checkpoint,
                resume=True,
            )
        )


def test_resume_restores_consumed_budget_before_new_provider_calls(tmp_path, monkeypatch):
    calls = []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        calls.append([case["case_id"] for case in cases])
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "checkpoint_version": "bailian-batch-checkpoint-v1",
                "identity": {"cases_sha256": "a", "batch_size": 1},
                "status": "RUNNING",
                "eligible_case_count": 2,
                "excluded_case_count": 0,
                "completed_batches": [
                    _completed_record("batch-001", ["a"], request_count=2, cost_units=2)
                ],
                "failed_batch": None,
                "request_count": 2,
                "cost_units": 2,
                "real_service_acceptance": False,
                "m1_connected": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MODULE.RequestBudgetExceeded, match="request_budget_exceeded:embedding"):
        asyncio.run(
            MODULE.run_batches(
                object(),
                _small_plan(),
                identity={"cases_sha256": "a", "batch_size": 1},
                max_requests=2,
                max_requests_per_minute=4,
                max_cost_units=2,
                document_batch_size=16,
                checkpoint_path=checkpoint,
                resume=True,
            )
        )
    assert calls == []


def test_resume_restores_rolling_rate_window(tmp_path, monkeypatch):
    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()

    async def sleep(delay):
        clock.now += delay

    calls = []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        calls.append([case["case_id"] for case in cases])
        return {"status": "PASS", "case_results": [{"case_id": cases[0]["case_id"], "status": "PASS"}]}

    class Provider:
        async def embed(self, texts):
            return []

    monkeypatch.setattr(MODULE.retrieval, "run_live", fake_run_live)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "checkpoint_version": "bailian-batch-checkpoint-v1",
                "identity": {"cases_sha256": "a", "batch_size": 1},
                "status": "RUNNING",
                "eligible_case_count": 2,
                "excluded_case_count": 0,
                "completed_batches": [
                    _completed_record("batch-001", ["a"], request_count=1, cost_units=1)
                ],
                "failed_batch": None,
                "request_count": 1,
                "cost_units": 1,
                "recent_request_timestamps": [50.0],
                "real_service_acceptance": False,
                "m1_connected": False,
            }
        ),
        encoding="utf-8",
    )
    result = asyncio.run(
        MODULE.run_batches(
            Provider(),
            _small_plan(),
            identity={"cases_sha256": "a", "batch_size": 1},
            max_requests=3,
            max_requests_per_minute=1,
            max_cost_units=3,
            document_batch_size=16,
            checkpoint_path=checkpoint,
            resume=True,
            clock=clock,
            sleep=sleep,
            wall_clock=clock,
        )
    )
    assert result["request_count"] == 2
    assert calls == [["b"]]
    assert clock.now == 110.0


def test_live_rejects_existing_report_before_provider_config_or_network(tmp_path):
    output = tmp_path / "existing-report.json"
    output.write_text("{}\n", encoding="utf-8")
    args = SimpleNamespace(
        cases=MODULE.DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.jsonl",
        metadata=MODULE.DEFAULT_REVISION_ROOT / "synthetic-m2-v1-revision-1.metadata.json",
        corpus=MODULE.DEFAULT_REVISION_ROOT / "documents.jsonl",
        alignment=MODULE.DEFAULT_REVISION_ROOT / "alignment-revision-1.json",
        answer_evidence=MODULE.DEFAULT_CONTEXT_REVIEW,
        batch_size=5,
        document_batch_size=16,
        output=output,
        live=True,
        env=tmp_path / "missing-provider.env",
        checkpoint=tmp_path / "checkpoint.json",
        resume=False,
        max_requests=240,
        max_requests_per_minute=60,
        max_cost_units=240,
    )
    assert asyncio.run(MODULE.main_async(args)) == 2


def test_failed_batch_retains_redacted_diagnostics_and_stops(tmp_path, monkeypatch):
    calls = []

    async def failing(provider, cases, corpus, alignment, **kwargs):
        assert kwargs["fail_fast"] is True
        calls.append(cases[0]["case_id"])
        return {"status": "FAIL", "issues": ["embedding:timeout", "sensitive vendor text"],
                "case_results": [{"case_id": cases[0]["case_id"], "status": "FAIL",
                                  "error_code": "schema_error", "raw_text": "sensitive response"}]}

    monkeypatch.setattr(MODULE.retrieval, "run_live", failing)
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(MODULE.BatchPlanError, match="batch_failed"):
        asyncio.run(MODULE.run_batches(
            object(), _small_plan(), identity={}, max_requests=4,
            max_requests_per_minute=4, max_cost_units=4, document_batch_size=16,
            checkpoint_path=checkpoint,
        ))
    assert calls == ["a"]
    saved = json.loads(checkpoint.read_text())
    assert saved["failed_batch"]["diagnostics"] == {
        "case_statuses": [{"case_id": "a", "status": "FAIL", "error_code": "schema_error"}],
        "issues": ["embedding:timeout", "unclassified_failure"],
    }
    assert "sensitive" not in checkpoint.read_text()


def test_checkpoint_report_preserves_legacy_failure_and_remaining_budget():
    plan = _small_plan()
    checkpoint = MODULE._checkpoint_payload({}, plan)
    checkpoint.update({
        "status": "FAILED", "request_count": 2, "cost_units": 2,
        "completed_batches": [_completed_record("batch-001", ["a"], request_count=1, cost_units=1)],
        "failed_batch": {"batch_id": "batch-002", "case_ids": ["b"], "error_code": "batch_failed", "reserved_request_count": 1},
    })
    report = MODULE.report_checkpoint(checkpoint, plan, {}, {
        "max_requests": 2, "max_cost_units": 2, "max_requests_per_minute": 2,
    })
    assert report["status"] == "FAILED"
    assert report["completed_case_count"] == report["unmeasured_case_count"] == 1
    assert report["new_provider_requests"] == 0
    assert report["actual_request_count"] == 2
    assert report["remaining_estimated_request_count"] == 1
    assert report["minimum_total_request_limit_for_resume"] == 3
    assert report["resume_within_current_limits"] is False
    assert report["failed_batch"]["failure_detail_status"] == "UNAVAILABLE_LEGACY_CHECKPOINT"
    checkpoint["completed_batches"][0]["case_results"][0]["status"] = "FAIL"
    with pytest.raises(MODULE.BatchPlanError, match="payload_hash_mismatch"):
        MODULE.report_checkpoint(checkpoint, plan, {}, {})


def test_offline_checkpoint_export_never_loads_provider_config(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Provider config or network access is forbidden")

    monkeypatch.setattr(MODULE, "ensure_secure_config_file", forbidden)
    monkeypatch.setattr(MODULE, "load_provider_config", forbidden)
    monkeypatch.setattr(MODULE, "build_provider", forbidden)
    plan = _small_plan()
    monkeypatch.setattr(MODULE, "build_plan", lambda *a, **kw: {**plan, "eligible_cases": [{}] * 53, "excluded_cases": [{}] * 7})
    monkeypatch.setattr(MODULE.retrieval, "load_inputs", lambda *a: ([], [], {}, []))
    monkeypatch.setattr(MODULE, "validate_review", lambda *a: ({}, {"status": "APPROVED", "approved_support_chunk_ids": {}}))
    monkeypatch.setattr(MODULE, "_identity", lambda *a, **kw: {})
    checkpoint = tmp_path / "checkpoint.json"
    data = MODULE._checkpoint_payload({}, {**plan, "eligible_cases": [{}] * 53, "excluded_cases": [{}] * 7})
    data.update({"status": "FAILED", "failed_batch": {"batch_id": "batch-001", "case_ids": ["a"], "error_code": "batch_failed", "reserved_request_count": 0}})
    checkpoint.write_text(json.dumps(data))
    original = checkpoint.read_bytes()
    args = SimpleNamespace(
        cases=None, corpus=None, alignment=None, metadata=None, answer_evidence=None,
        batch_size=1, document_batch_size=16, report_checkpoint=True, live=False,
        checkpoint=checkpoint, output=tmp_path / "report.json",
        max_requests=4, max_requests_per_minute=4, max_cost_units=4,
    )
    assert asyncio.run(MODULE.main_async(args)) == 0
    assert checkpoint.read_bytes() == original
    report = json.loads(args.output.read_text())
    assert report["status"] == "FAILED" and report["new_provider_requests"] == 0
    assert args.output.stat().st_mode & 0o777 == 0o600
    assert asyncio.run(MODULE.main_async(args)) == 2
