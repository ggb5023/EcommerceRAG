import asyncio
import importlib.util
import json
from pathlib import Path

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
        usage = {"total_tokens": 1}

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
        structured = {"answer_points": ["supported point"]}
        model = "model"
        request_id = "request"
        usage = {"total_tokens": 1}

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
        "expected_answer_points": ["supported point"],
        "authorization": {"tenant_id": "tenant-a", "shop_id": "shop-a"},
    }]
    corpus = [{
        "document_id": "actual-doc",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "document_id": "actual-doc",
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
        usage = {"total_tokens": 1}
        items = [
            type("Item", (), {"index": 1, "score": 0.99})(),
            type("Item", (), {"index": 0, "score": 0.01})(),
        ]

    class Generation:
        structured = {"answer_points": ["reranked evidence"]}
        model = "model"
        request_id = "request"
        usage = {"total_tokens": 1}

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
    }]
    corpus = [{
        "document_id": "doc-a",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-a",
            "document_id": "doc-a",
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
