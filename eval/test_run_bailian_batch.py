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


def test_resume_restores_consumed_budget_before_new_provider_calls(tmp_path, monkeypatch):
    calls = []

    async def fake_run_live(provider, cases, corpus, alignment, **kwargs):
        await provider.embed(["fixture"])
        calls.append([case["case_id"] for case in cases])
        return {"status": "PASS", "case_results": []}

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
                    {
                        "batch_id": "batch-001",
                        "case_ids": ["a"],
                        "status": "PASS",
                        "request_count": 2,
                        "cost_units": 2,
                        "slot_reports": [],
                        "case_results": [],
                    }
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
        return {"status": "PASS", "case_results": []}

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
                    {"batch_id": "batch-001", "case_ids": ["a"], "status": "PASS", "slot_reports": [], "case_results": []}
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
