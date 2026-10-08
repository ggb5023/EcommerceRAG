import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

SPEC = importlib.util.spec_from_file_location(
    "run_bailian_retrieval", Path(__file__).with_name("run_bailian_retrieval.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_cosine_is_stable_and_zero_safe():
    assert MODULE.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert MODULE.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert MODULE.cosine([1.0], [1.0, 0.0]) == 0.0


def test_token_overlap_is_diagnostic_and_thresholded():
    assert MODULE._token_overlap_supported("配送需要 2-5 个工作日", "配送通常需要 2-5 个工作日")
    assert not MODULE._token_overlap_supported("配送需要 2-5 个工作日", "完全不同的商品规格")


def test_answer_point_diagnostics_distinguish_evidence_and_generation_gaps():
    diagnostics = MODULE._answer_point_diagnostics(
        ["配送需要 2-5 个工作日", "规格需要人工确认", "未提供的事实"],
        ["配送通常需要 2-5 个工作日", "完全不同的回答"],
        "标准配送需要 2-5 个工作日。规格需要人工确认。",
    )
    assert diagnostics["generated_answer_point_count"] == 2
    assert all(len(value) == 12 for value in diagnostics["expected_answer_point_fingerprints"])
    assert all(len(value) == 12 for value in diagnostics["generated_answer_point_fingerprints"])
    assert diagnostics["evidence_token_overlap_answer_point_match_count"] == 2
    assert diagnostics["generated_token_overlap_answer_point_match_count"] == 1
    assert diagnostics["answer_point_diagnostic_counts"] == {
        "generated_supported": 1,
        "generation_rewrite_mismatch": 1,
        "evidence_unsupported": 1,
    }
    empty = MODULE._answer_point_diagnostics(["可回答的事实"], [], "可回答的事实")
    assert empty["answer_point_diagnostic_counts"] == {"generation_empty": 1}
    assert all("配送" not in json.dumps(diagnostics, ensure_ascii=False) for _ in [0])


def test_reviewed_evidence_separates_source_alignment_from_generation_rewrite():
    diagnostics = MODULE._answer_point_diagnostics(
        ["适合20至26摄氏度且应保持干燥"],
        ["建议在室内约20至26摄氏度、干燥且通风正常"],
        "商品资料建议在室内约 20 至 26 摄氏度、干燥且通风正常的环境中使用。",
        [True],
        [True],
    )
    assert diagnostics["evidence_exact_answer_point_match_count"] == 0
    assert diagnostics["evidence_token_overlap_answer_point_match_count"] == 0
    assert diagnostics["approved_evidence_answer_point_match_count"] == 1
    assert diagnostics["evidence_supported_answer_point_count"] == 1
    assert diagnostics["answer_point_diagnostic_counts"] == {"generation_rewrite_mismatch": 1}


def test_composition_diagnostics_detect_split_points_without_claiming_quality():
    diagnostics = MODULE._answer_point_diagnostics(
        ["The compact kettle weighs only 300 grams, measures 20 centimeters high, holds 500 milliliters, and uses a 220 volt supply."],
        ["weighs only 300 grams", "measures 20 centimeters high", "holds 500 milliliters", "uses a 220 volt supply"],
        "",
    )
    row = diagnostics["answer_point_composition_diagnostics"][0]
    assert diagnostics["answer_point_composition_diagnostic_version"] == "answer-point-composition-v1"
    assert diagnostics["answer_point_composition_is_heuristic"] is True
    assert row["split_across_generated_points_candidate"] is True
    assert diagnostics["expected_points_with_split_overlap_candidate_count"] == 1
    assert row["language_relation"] == "same_script"
    assert row["expected_numeric_literal_count"] == row["matched_numeric_literal_count"] == 4
    assert row["expected_unit_category_count"] == row["matched_unit_category_count"] == 3
    assert diagnostics["answer_point_composition_is_heuristic"] is True
    assert "travel kettle" not in json.dumps(diagnostics)


def test_composition_diagnostics_detect_one_point_combining_multiple_expected_points():
    diagnostics = MODULE._answer_point_diagnostics(
        ["Standard delivery takes 2-5 business days", "Returns are allowed within 7 calendar days"],
        ["Standard delivery takes 2-5 business days and returns are allowed within 7 calendar days"],
        "",
    )
    assert diagnostics["generated_points_with_multi_expected_overlap_candidate_count"] == 1
    assert diagnostics["aggregate_generated_token_overlap_answer_point_count"] == 2
    assert all(row["language_relation"] == "same_script" for row in diagnostics["answer_point_composition_diagnostics"])


def test_composition_diagnostics_flags_language_and_qualifier_differences_as_counts():
    diagnostics = MODULE._answer_point_diagnostics(
        ["仅未拆封商品可以退货，签收后7天内申请"],
        ["Unopened products can be returned within 7 days after delivery."],
        "",
    )
    row = diagnostics["answer_point_composition_diagnostics"][0]
    assert row["language_relation"] == "opposite_script"
    assert row["expected_qualifier_marker_count"] > 0
    assert row["missing_qualifier_marker_count"] > 0
    assert row["expected_numeric_literal_count"] == row["matched_numeric_literal_count"] == 1
    assert diagnostics["answer_point_composition_is_heuristic"] is True
    assert diagnostics["language_relation_counts"]["opposite_script"] == 1


def test_existing_synthetic_case_can_be_diagnosed_when_fake_output_splits_a_point():
    cases = MODULE.load_jsonl(MODULE.DEFAULT_CASES)
    candidates = [
        point
        for case in cases
        for point in case.get("expected_answer_points", [])
        if isinstance(point, str) and len(point) >= 15
    ]
    split_diagnostics = None
    for point in candidates:
        first = len(point) // 3
        second = 2 * len(point) // 3
        fragments = [point[:first], point[first:second], point[second:]]
        row = MODULE._answer_point_composition_diagnostics([point], fragments)["answer_point_composition_diagnostics"][0]
        if row["split_across_generated_points_candidate"]:
            split_diagnostics = row
            break
    assert split_diagnostics is not None
    assert split_diagnostics["individual_generated_point_overlap_count"] == 0
    assert split_diagnostics["aggregate_generated_point_overlap"] is True


def test_no_live_flag_is_explicitly_not_run(tmp_path):
    cases = tmp_path / "cases.jsonl"
    corpus = tmp_path / "corpus.jsonl"
    alignment = tmp_path / "alignment.json"
    cases.write_text("{}\n", encoding="utf-8")
    corpus.write_text("{}\n", encoding="utf-8")
    alignment.write_text(json.dumps({}), encoding="utf-8")
    report = MODULE.not_run_report(cases, corpus, alignment, ["live_flag_required"])
    assert report["status"] == "NOT_RUN"
    assert report["real_service_acceptance"] is False
    assert report["online_requests_made"] is False


def test_embedding_ranking_does_not_store_content():
    chunks = [{"chunk_id": "a", "document_id": "doc-a"}, {"chunk_id": "b", "document_id": "doc-b"}]
    rows = MODULE._rank_by_embedding([1.0, 0.0], [[0.9, 0.0], [0.0, 1.0]], chunks, 2)
    assert rows[0]["document_id"] == "doc-a"
    assert all("content" not in row for row in rows)


def test_metadata_is_redacted():
    class Result:
        model = "secret-model-id"
        request_id = "full-request-id"
        usage: ClassVar[dict[str, int]] = {"total_tokens": 1}

    metadata = MODULE._meta("embedding", [Result()])
    assert metadata["metadata_complete"] is True
    assert metadata["model_fingerprint"] != Result.model
    assert metadata["request_id_fingerprint"] != Result.request_id


def test_live_runner_keeps_provider_payloads_out_of_report():
    class Embedding:
        def __init__(self, dense, request_id="request"):
            self.dense = tuple(dense)
            self.model = "model"
            self.request_id = request_id
            self.usage = {"total_tokens": 1}

    class Rerank:
        def __init__(self):
            self.items = [type("Item", (), {"index": 0, "score": 0.9})()]
            self.model = "model"
            self.request_id = "request"
            self.usage = {"total_tokens": 1}

    class Generation:
        structured: ClassVar[dict[str, list[str]]] = {"answer_points": ["product evidence"]}
        model = "model"
        request_id = "request"
        usage: ClassVar[dict[str, int]] = {"total_tokens": 1}

    class Provider:
        def __init__(self):
            self.generation_messages = []

        async def embed(self, texts, **kwargs):
            return [Embedding([1.0, 0.0]) for _ in texts]

        async def rerank(self, query, candidates, **kwargs):
            return Rerank()

        async def generate(self, messages, **kwargs):
            self.generation_messages.append(messages)
            return Generation()

    cases = [{
        "case_id": "case-1",
        "query": "product query",
        "expected_doc_ids": ["label-doc"],
        "expected_answer_points": ["product evidence"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a"},
        "business_date": "2026-10-01",
    }]
    corpus = [{
        "document_id": "actual-doc",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "document_id": "actual-doc",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "content": "product evidence",
            "disclosure_class": "external_allowed",
        }],
    }]
    alignment = {"case_to_source_documents": {"case-1": {"label-doc": "actual-doc"}}}
    provider = Provider()
    report = asyncio.run(MODULE.run_live(provider, cases, corpus, alignment))
    assert report["status"] == "PASS"
    assert report["case_results"][0]["rerank_hit_at_5"] is True
    assert all("content" not in row for row in report["case_results"])
    assert report["case_results"][0]["normalized_answer_point_match_count"] == 1
    assert report["case_results"][0]["generated_answer_point_count"] == 1
    assert report["case_results"][0]["answer_point_diagnostic_counts"] == {"generated_supported": 1}
    prompt = provider.generation_messages[0][0]["content"]
    assert MODULE.GENERATION_INSTRUCTION in prompt
    assert "product evidence" in prompt


def test_generation_evidence_uses_rerank_order():
    class Embedding:
        def __init__(self, dense):
            self.dense = tuple(dense)
            self.model = "model"
            self.request_id = "request"
            self.usage = {"total_tokens": 1}

    class Rerank:
        model = "model"
        request_id = "request"
        usage: ClassVar[dict[str, int]] = {"total_tokens": 1}
        items: ClassVar[list[object]] = [
            type("Item", (), {"index": 1, "score": 0.99})(),
            type("Item", (), {"index": 0, "score": 0.01})(),
        ]

    class Generation:
        structured: ClassVar[dict[str, list[str]]] = {"answer_points": ["reranked evidence"]}
        model = "model"
        request_id = "request"
        usage: ClassVar[dict[str, int]] = {"total_tokens": 1}

    class Provider:
        def __init__(self):
            self.prompt = ""

        async def embed(self, texts, **kwargs):
            return [Embedding([1.0, 0.0]) for _ in texts]

        async def rerank(self, query, candidates, **kwargs):
            return Rerank()

        async def generate(self, messages, **kwargs):
            self.prompt = messages[0]["content"]
            return Generation()

    provider = Provider()
    cases = [{
        "case_id": "case-1",
        "query": "product query",
        "expected_answer_points": ["reranked evidence"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a"},
        "business_date": "2026-10-01",
    }]
    corpus = [{
        "document_id": "doc-a",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-a",
            "document_id": "doc-a",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "content": "embedding-first evidence",
            "disclosure_class": "external_allowed",
        }],
    }, {
        "document_id": "doc-b",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-b",
            "document_id": "doc-b",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "content": "reranked evidence",
            "disclosure_class": "external_allowed",
        }],
    }]
    report = asyncio.run(MODULE.run_live(provider, cases, corpus, {
        "case_to_source_documents": {"case-1": {"label": "doc-b"}},
    }))
    assert report["status"] == "PASS"
    assert "reranked evidence" in provider.prompt
    assert provider.prompt.index("reranked evidence") < provider.prompt.index("embedding-first evidence")


def _case(case_id="case-1", *, tenant="tenant-a", shop="shop-a"):
    return {
        "case_id": case_id,
        "query": case_id,
        "expected_answer_points": ["source evidence"],
        "authorization": {"tenant_id": tenant, "shop_id": shop},
        "business_date": "2026-10-01",
    }


def _document(document_id, *, tenant="tenant-a", shop="shop-a", **policy):
    scope = {"tenant_id": tenant, "shop_id": shop}
    return {
        "document_id": document_id,
        "document_version_id": f"version-{document_id}",
        **scope,
        "chunks": [{
            "chunk_id": f"chunk-{document_id}",
            "document_id": document_id,
            "document_version_id": f"version-{document_id}",
            "chunk_hash": "f" * 64,
            **scope,
            "content": f"source evidence {document_id}",
            "disclosure_class": "external_allowed",
            **policy,
        }],
    }


class _RecordingProvider:
    def __init__(self, rerank_indices=None, generated_points=None):
        self.rerank_indices = rerank_indices
        self.generated_points = generated_points or ["source evidence"]
        self.calls = []
        self.prompts = []
        self.candidates = []

    async def embed(self, texts, **kwargs):
        self.calls.append("embedding")
        return [SimpleNamespace(dense=(1.0, 0.0), model="model", request_id="request", usage={}) for _ in texts]

    async def rerank(self, query, candidates, **kwargs):
        self.calls.append("rerank")
        self.candidates.append(candidates)
        indices = range(min(kwargs["top_n"], len(candidates))) if self.rerank_indices is None else self.rerank_indices
        return SimpleNamespace(
            items=[SimpleNamespace(index=index, score=0.9) for index in indices],
            model="model", request_id="request", usage={},
        )

    async def generate(self, messages, **kwargs):
        self.calls.append("generation")
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(
            structured={"answer_points": self.generated_points},
            model="model", request_id="request", usage={},
        )


def test_fake_provider_reports_composition_diagnostics_without_answer_text():
    case = {
        **_case(),
        "expected_answer_points": [
            "The compact kettle weighs only 300 grams, measures 20 centimeters high, holds 500 milliliters, and uses a 220 volt supply."
        ],
    }
    provider = _RecordingProvider(generated_points=[
        "weighs only 300 grams",
        "measures 20 centimeters high",
        "holds 500 milliliters",
        "uses a 220 volt supply",
    ])
    report = asyncio.run(MODULE.run_live(provider, [case], [_document("a")], {}))
    row = report["case_results"][0]
    assert row["answer_point_composition_diagnostics"][0]["split_across_generated_points_candidate"] is True
    assert row["answer_quality_status"] == "NOT_RUN"
    encoded = json.dumps(report)
    assert "compact kettle" not in encoded
    assert "300 grams" not in encoded
    assert "centimeters high" not in encoded


def test_batch_cases_keep_each_tenant_and_shop_candidates_separate():
    provider = _RecordingProvider()
    cases = [_case("case-a"), _case("case-b", tenant="tenant-b"), _case("case-c", shop="shop-b")]
    corpus = [_document("doc-a"), _document("doc-b", tenant="tenant-b"), _document("doc-c", shop="shop-b")]
    report = asyncio.run(MODULE.run_live(provider, cases, corpus, {}))
    assert report["status"] == "PASS"
    for index, document_id in enumerate(("doc-a", "doc-b", "doc-c")):
        row = report["case_results"][index]
        assert {result["document_id"] for result in row["embedding_top5"]} == {document_id}
        assert provider.candidates[index] == [f"source evidence {document_id}"]
        assert row["generation_evidence"][0]["document_id"] == document_id
        for other_id in {"doc-a", "doc-b", "doc-c"} - {document_id}:
            assert other_id not in provider.prompts[index]


def test_expired_future_and_internal_chunks_never_reach_provider():
    provider = _RecordingProvider()
    corpus = [
        _document("current", effective_from="2026-10-01", effective_to="2026-10-02"),
        _document("expired", effective_to="2026-10-01"),
        _document("future", effective_from="2026-10-02"),
        _document("internal", disclosure_class="internal_only"),
        _document("unclassified", disclosure_class="unclassified"),
    ]
    report = asyncio.run(MODULE.run_live(provider, [_case()], corpus, {}))
    assert report["status"] == "PASS"
    assert provider.candidates == [["source evidence current"]]
    assert [row["document_id"] for row in report["case_results"][0]["generation_evidence"]] == ["current"]
    assert all(word not in provider.prompts[0] for word in ("expired", "future", "internal", "unclassified"))


def test_document_expiry_and_chunk_scope_drift_cannot_bypass_filter():
    document = _document("expired-document")
    document["effective_to"] = "2026-10-01"
    other = _document("drifted-chunk")
    other["chunks"][0]["shop_id"] = "shop-other"
    provider = _RecordingProvider()
    report = asyncio.run(MODULE.run_live(provider, [_case()], [document, other], {}))
    assert report["status"] == "FAIL"
    assert report["issues"] == ["no_eligible_chunks"]
    assert provider.calls == []


def test_partial_rerank_does_not_append_unreturned_candidates_or_approve_missing_evidence():
    provider = _RecordingProvider(rerank_indices=[0])
    report = asyncio.run(MODULE.run_live(
        provider, [_case()], [_document("a"), _document("b")], {},
        answer_evidence={"case-1": [True]},
        answer_evidence_chunks={"case-1": [["chunk-a", "chunk-b"]]},
    ))
    row = report["case_results"][0]
    assert [item["chunk_id"] for item in row["generation_evidence"]] == ["chunk-a"]
    assert "doc-b" not in provider.prompts[0]
    assert "source evidence b" not in provider.prompts[0]
    assert row["approved_evidence_answer_point_match_count"] == 1
    assert row["delivered_approved_evidence_answer_point_match_count"] == 0
    assert row["approved_support_chunk_coverage"] == [{
        "point_index": 0, "required_chunk_count": 2, "delivered_chunk_count": 1, "all_delivered": False,
    }]
    assert row["evidence_supported_answer_point_count"] == 0
    assert row["answer_point_diagnostic_counts"] == {"approved_evidence_not_delivered": 1}
    assert row["answer_quality_status"] == "NOT_RUN"


def test_reviewed_support_requires_all_bound_chunks_in_generation_context():
    provider = _RecordingProvider()
    report = asyncio.run(MODULE.run_live(
        provider, [_case()], [_document("a"), _document("b")], {},
        answer_evidence={"case-1": [True]},
        answer_evidence_chunks={"case-1": [["chunk-a", "chunk-b"]]},
    ))
    row = report["case_results"][0]
    assert row["delivered_approved_evidence_answer_point_match_count"] == 1
    assert row["answer_point_diagnostic_counts"] == {"generated_supported": 1}
    packed = json.dumps(report)
    assert "source evidence" not in packed
    assert "Query:" not in packed
    assert row["answer_quality_status"] == "NOT_RUN"


def test_review_flags_without_chunk_binding_cannot_claim_delivered_evidence():
    diagnostics = MODULE._answer_point_diagnostics(["source evidence"], ["source evidence"], "source evidence", [True])
    assert diagnostics["approved_evidence_answer_point_match_count"] == 1
    assert diagnostics["delivered_approved_evidence_answer_point_match_count"] == 0
    assert diagnostics["answer_point_diagnostic_counts"] == {"approved_evidence_not_delivered": 1}


def test_support_binding_larger_than_top_five_is_not_claimed_as_delivered():
    provider = _RecordingProvider()
    corpus = [_document(str(index)) for index in range(6)]
    report = asyncio.run(MODULE.run_live(
        provider, [_case()], corpus, {},
        answer_evidence={"case-1": [True]},
        answer_evidence_chunks={"case-1": [[f"chunk-{index}" for index in range(6)]]},
    ))
    row = report["case_results"][0]
    assert len(row["generation_evidence"]) == 5
    assert row["approved_support_chunk_coverage"] == [{
        "point_index": 0, "required_chunk_count": 6, "delivered_chunk_count": 5, "all_delivered": False,
    }]
    assert row["answer_point_diagnostic_counts"] == {"approved_evidence_not_delivered": 1}


def test_empty_rerank_does_not_generate_from_unranked_candidates():
    provider = _RecordingProvider(rerank_indices=[])
    report = asyncio.run(MODULE.run_live(provider, [_case()], [_document("a")], {}))
    assert report["status"] == "FAIL"
    assert report["case_results"][0]["error_code"] == "no_rerank_evidence"
    assert "generation" not in provider.calls


@pytest.mark.parametrize("provider_failure", [False, True])
def test_fail_fast_stops_later_cases_on_provider_or_evidence_failure(provider_failure):
    class Provider(_RecordingProvider):
        async def generate(self, messages, **kwargs):
            raise MODULE.ProviderError("schema_error", "sensitive vendor error")

    provider = Provider() if provider_failure else _RecordingProvider(rerank_indices=[])
    report = asyncio.run(MODULE.run_live(
        provider, [_case("first"), _case("later")], [_document("a")], {}, fail_fast=True,
    ))
    assert report["status"] == "FAIL"
    assert [row["case_id"] for row in report["case_results"]] == ["first"]
    assert provider.calls.count("rerank") == 1
    assert "sensitive vendor error" not in json.dumps(report)


@pytest.mark.parametrize("invalid_date", [None, "20261001", "2026-02-30", "", True])
def test_invalid_business_date_fails_before_provider_calls(invalid_date):
    provider = _RecordingProvider()
    case = {**_case(), "business_date": invalid_date}
    with pytest.raises(ValueError):
        asyncio.run(MODULE.run_live(provider, [case], [_document("a")], {}))
    assert provider.calls == []


def test_invalid_effective_date_is_reported_as_input_integrity_issue():
    document = _document("a", effective_from="2026-10-02", effective_to="2026-10-01")
    assert MODULE.validate_corpus_versions([document]) == ["corpus_chunk_dates_invalid:chunk-a"]


def test_context_audit_reports_budget_without_selecting_evidence_or_leaking_text():
    corpus = [_document(str(index)) for index in range(6)]
    bindings = {"case-1": [[f"chunk-{index}" for index in range(6)]]}
    report = MODULE.audit_generation_context([_case()], corpus, bindings)
    assert report["status_counts"]["BUDGET_EXCEEDED"] == 1
    assert report["case_results"][0]["required_chunk_count"] == 6
    assert report["online_requests_made"] is False
    assert report["generation_context_selection"] == "NOT_RUN"
    assert report["answer_quality_status"] == "NOT_RUN"
    assert "source evidence" not in json.dumps(report)


def test_context_audit_uses_union_of_point_bindings_and_checks_eligibility():
    case = {**_case(), "expected_answer_points": ["point-a", "point-b"]}
    corpus = [_document("a"), _document("b", effective_to="2026-10-01")]
    report = MODULE.audit_generation_context([case], corpus, {"case-1": [["chunk-a"], ["chunk-a", "chunk-b"]]})
    row = report["case_results"][0]
    assert row["status"] == "INELIGIBLE_REQUIRED_CHUNKS"
    assert row["required_chunk_count"] == 2
    assert row["ineligible_required_chunk_ids"] == ["chunk-b"]
    assert row["point_required_chunk_counts"] == [1, 2]
    report = MODULE.audit_generation_context([case], corpus, {"case-1": [["chunk-a"], ["chunk-a"]]})
    assert report["case_results"][0]["status"] == "WITHIN_BUDGET"
    assert report["case_results"][0]["required_chunk_count"] == 1


def test_context_audit_does_not_infer_approval_for_missing_point_bindings():
    case = {**_case(), "expected_answer_points": ["point-a", "point-b"]}
    report = MODULE.audit_generation_context([case], [_document("a")], {"case-1": [["chunk-a"]]})
    assert report["case_results"][0]["status"] == "UNREVIEWED"


def test_report_is_restricted_atomic_and_preserves_existing_evidence(tmp_path):
    output = tmp_path / "reports" / "report.json"
    MODULE.write_report(output, {"status": "NOT_RUN"})
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        MODULE.write_report(output, {"status": "PASS"})
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


def test_report_encoding_failure_leaves_no_partial_file(tmp_path):
    output = tmp_path / "report.json"
    with pytest.raises(TypeError):
        MODULE.write_report(output, {"not_json": object()})
    assert list(tmp_path.iterdir()) == []


def test_cli_context_audit_does_not_load_provider_configuration(tmp_path, monkeypatch, capsys):
    paths = {name: tmp_path / name for name in ("cases", "corpus", "alignment", "metadata", "answer_evidence")}
    for path in paths.values():
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(MODULE, "load_inputs", lambda *args: ([_case()], [_document("a")], {}, []))
    monkeypatch.setattr(MODULE, "validate_review", lambda *args: ({"case-1": [True]}, {"approved_support_chunk_ids": {"case-1": [["chunk-a"]]}}))
    def forbidden(*args):
        raise AssertionError("offline audit must not read configuration or build Provider")
    monkeypatch.setattr(MODULE, "load_provider_config", forbidden)
    monkeypatch.setattr(MODULE, "build_provider", forbidden)
    args = SimpleNamespace(**paths, output=None, env=tmp_path / "missing.env", live=False, audit_context=True, limit=1)
    assert asyncio.run(MODULE.main_async(args)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status_counts"]["WITHIN_BUDGET"] == 1
    assert report["online_requests_made"] is False


def test_cli_rejects_existing_output_before_provider_calls(tmp_path, monkeypatch, capsys):
    output = tmp_path / "prior-report.json"
    output.write_text("historical evidence", encoding="utf-8")
    monkeypatch.setattr(MODULE, "load_inputs", lambda *args: ([_case()], [_document("a")], {}, []))
    def forbidden(*args):
        raise AssertionError("output conflict must not call Provider")
    monkeypatch.setattr(MODULE, "load_provider_config", forbidden)
    args = SimpleNamespace(cases=output, corpus=output, alignment=output, metadata=output, answer_evidence=None, output=output, live=True)
    assert asyncio.run(MODULE.main_async(args)) == 2
    assert json.loads(capsys.readouterr().out)["issues"] == ["output_exists"]
    assert output.read_text(encoding="utf-8") == "historical evidence"
