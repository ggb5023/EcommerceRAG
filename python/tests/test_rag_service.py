#!/usr/bin/env python3
import os
import subprocess
import sys
import unittest
import grpc
from pathlib import Path

from app.rag_service import FIXTURES, RagService, rag_pb2
from app.authorization import ScopeError


class Ctx:
    def __init__(self, active=True):
        self.active = active
        self.code = None

    def abort(self, code, details):
        self.code = code
        raise RuntimeError(details)

    def is_active(self):
        return self.active


def request_context(**kwargs):
    defaults = {"request_id": "r1", "tenant_id": "m1-tenant", "user_id": "m1-user",
                "shop_id": "shop-demo", "allowed_shop_ids": ["shop-demo"],
                "permission_revision": "auth-v1", "role": "operator",
                "enforce_document_scope": True,
                "allowed_document_ids": [f["document_id"] for f in FIXTURES]}
    defaults.update(kwargs)
    return rag_pb2.RequestContext(**defaults)


def fixture_evidence():
    f = FIXTURES[0]
    return rag_pb2.Evidence(**{k: f[k] for k in
        ("id", "content", "source_type", "source_ref", "document_id", "version_id", "shop_id", "tenant_id", "disclosure_class")},
        customer_eligible=True)


class RagServiceTests(unittest.TestCase):
    def test_empty_document_scope_grants_no_evidence(self):
        result = list(RagService(scope_validator=lambda scope: None).Search(
            rag_pb2.SearchRequest(context=request_context(allowed_document_ids=[]), query="配送"), Ctx()))
        self.assertEqual(sum(len(part.evidence) for part in result), 0)

    def test_generate_rejects_content_not_bound_to_original_chunk(self):
        evidence = fixture_evidence()
        evidence.content = "forged synthetic content"
        with self.assertRaises(RuntimeError):
            list(RagService(scope_validator=lambda scope: None).Generate(
                rag_pb2.GenerateRequest(context=request_context(), evidence=[evidence]), Ctx()))

    def test_revision_is_checked_before_each_generated_event(self):
        calls = 0
        def authority(scope):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise ScopeError(grpc.StatusCode.PERMISSION_DENIED)
        context = Ctx()
        stream = RagService(scope_validator=authority).Generate(
            rag_pb2.GenerateRequest(context=request_context(), evidence=[fixture_evidence()]), context)
        self.assertTrue(next(stream).delta)
        with self.assertRaises(RuntimeError):
            next(stream)
        self.assertEqual(context.code, grpc.StatusCode.PERMISSION_DENIED)

    def test_understand_routes_knowledge_and_facts(self):
        service = RagService(scope_validator=lambda scope: None)
        knowledge = service.Understand(rag_pb2.UnderstandRequest(
            context=request_context(), query="退货规则"), Ctx())
        fact = service.Understand(rag_pb2.UnderstandRequest(
            context=request_context(), query="当前库存"), Ctx())
        self.assertEqual(knowledge.intent, "knowledge")
        self.assertEqual(fact.information_source, "facts")
        self.assertEqual(knowledge.reason, "deterministic_m1_router")

    def test_understand_requests_clarification_for_vague_query(self):
        response = RagService(scope_validator=lambda scope: None).Understand(rag_pb2.UnderstandRequest(
            context=request_context(), query="这个"), Ctx())
        self.assertEqual(response.intent, "clarification")
        self.assertLess(response.confidence, 0.5)
        self.assertTrue(response.clarification)
        self.assertEqual(response.reason, "m1_missing_subject")

    def test_understand_requests_clarification_for_vague_followup_without_history(self):
        response = RagService(scope_validator=lambda scope: None).Understand(rag_pb2.UnderstandRequest(
            context=request_context(), query="那个怎么样？"), Ctx())
        self.assertEqual(response.intent, "clarification")
        self.assertEqual(response.reason, "m1_missing_subject")

    def test_vague_followup_uses_explicit_history(self):
        response = RagService(scope_validator=lambda scope: None).Understand(rag_pb2.UnderstandRequest(
            context=request_context(), query="那个怎么样？", history_summary="user: 保温杯容量是多少？"), Ctx())
        self.assertEqual(response.intent, "knowledge")
        self.assertIn("保温杯容量", response.rewritten_query)

    def test_search_returns_versioned_fixture_and_request_correlation(self):
        responses = list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(
            context=request_context(), query="配送"), Ctx()))
        self.assertTrue(responses[-1].complete)
        self.assertEqual(responses[0].evidence[0].document_id, "m1-demo-shipping")
        self.assertEqual(responses[0].evidence[0].version_id, FIXTURES[0]["version_id"])
        self.assertIn("M1 隔离验证", responses[0].evidence[0].content)
        self.assertEqual(responses[0].request_id, "r1")
        self.assertTrue(responses[0].is_mock)

    def test_search_selects_different_deterministic_fixtures(self):
        bottle = list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(
            context=request_context(), query="保温杯容量是多少"), Ctx()))
        care = list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(
            context=request_context(), query="毛巾可以烘干吗"), Ctx()))
        self.assertEqual(bottle[0].evidence[0].document_id, "m1-demo-bottle")
        self.assertEqual(care[0].evidence[0].document_id, "m1-demo-care")

    def test_search_returns_no_evidence_for_unknown_query(self):
        responses = list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(
            context=request_context(), query="未覆盖的问题"), Ctx()))
        self.assertTrue(responses[-1].complete)
        self.assertEqual(sum(len(response.evidence) for response in responses), 0)

    def test_search_rejects_cross_tenant_and_cross_shop_scope(self):
        service = RagService(scope_validator=lambda scope: None)
        other_tenant = list(service.Search(rag_pb2.SearchRequest(
            context=request_context(tenant_id="other"), query="配送"), Ctx()))
        self.assertEqual(sum(len(response.evidence) for response in other_tenant), 0)
        responses = list(service.Search(rag_pb2.SearchRequest(
            context=request_context(shop_id="other", allowed_shop_ids=["other"]), query="配送"), Ctx()))
        self.assertEqual(sum(len(response.evidence) for response in responses), 0)

    def test_search_rejects_missing_revision_or_empty_scope(self):
        service = RagService(scope_validator=lambda scope: None)
        for context in [request_context(permission_revision=""), request_context(allowed_shop_ids=[], all_shops=False),
                        request_context(user_id=""), request_context(shop_id="other")]:
            with self.assertRaises(RuntimeError):
                list(service.Search(rag_pb2.SearchRequest(context=context, query="配送"), Ctx()))

    def test_all_shops_requires_owner_or_admin(self):
        with self.assertRaises(RuntimeError):
            list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(
                context=request_context(all_shops=True), query="配送"), Ctx()))

    def test_generate_rejects_invalid_scope_and_filters_unauthorized_shop(self):
        invalid = rag_pb2.GenerateRequest(context=request_context(permission_revision=""), query="x")
        with self.assertRaises(RuntimeError):
            list(RagService(scope_validator=lambda scope: None).Generate(invalid, Ctx()))
        request = rag_pb2.GenerateRequest(context=request_context(), query="x", evidence=[
            fixture_evidence(),
            rag_pb2.Evidence(id="west", content="west secret", shop_id="shop-west",
                             disclosure_class="external_allowed", customer_eligible=True),
        ])
        events = list(RagService(scope_validator=lambda scope: None).Generate(request, Ctx()))
        self.assertIn(FIXTURES[0]["content"], events[0].delta)
        self.assertNotIn("west secret", events[0].delta)

    def test_generate_filters_internal_unclassified_and_ineligible_evidence(self):
        request = rag_pb2.GenerateRequest(context=request_context(), query="x", evidence=[
            fixture_evidence(),
            rag_pb2.Evidence(id="private", content="内部秘密", source_type="internal",
                             shop_id="shop-demo", disclosure_class="internal_only", customer_eligible=False),
            rag_pb2.Evidence(id="unknown", content="未分类秘密", source_type="faq",
                             shop_id="shop-demo", disclosure_class="unclassified", customer_eligible=True),
            rag_pb2.Evidence(id="ineligible", content="无资格内容", source_type="faq",
                             shop_id="shop-demo", disclosure_class="external_allowed", customer_eligible=False),
        ])
        events = list(RagService(scope_validator=lambda scope: None).Generate(request, Ctx()))
        self.assertIn(FIXTURES[0]["content"], events[0].delta)
        self.assertNotIn("内部秘密", events[0].delta)
        self.assertNotIn("未分类秘密", events[0].delta)
        self.assertNotIn("无资格内容", events[0].delta)
        citations = [event.citation.id for event in events if event.WhichOneof("event") == "citation"]
        self.assertEqual(citations, [FIXTURES[0]["id"]])
        self.assertTrue(any(event.WhichOneof("event") == "is_mock" and event.is_mock for event in events))
        self.assertTrue(any(event.WhichOneof("event") == "can_copy" and not event.can_copy for event in events))

    def test_missing_tenant_is_rejected(self):
        with self.assertRaises(RuntimeError):
            list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(query="x"), Ctx()))
        with self.assertRaises(RuntimeError):
            RagService(scope_validator=lambda scope: None).Understand(rag_pb2.UnderstandRequest(query="x"), Ctx())

    def test_cancelled_generate_stops_without_output(self):
        request = rag_pb2.GenerateRequest(context=request_context(), query="x")
        self.assertEqual(list(RagService(scope_validator=lambda scope: None).Generate(request, Ctx(active=False))), [])

    def test_synthetic_manifest_switch_uses_local_index(self):
        root = Path(__file__).resolve().parents[2]
        script = """
from app.rag_service import RagService, rag_pb2
class C:
    def abort(self, code, details): raise RuntimeError(details)
    def is_active(self): return True
ctx = rag_pb2.RequestContext(request_id='synthetic', tenant_id='demo-tenant-a', user_id='u', shop_id='demo-shop-east', allowed_shop_ids=['demo-shop-east'], role='operator', permission_revision='auth-v1', enforce_document_scope=True, allowed_document_ids=['syn-products-a'])
items = list(RagService(scope_validator=lambda scope: None).Search(rag_pb2.SearchRequest(context=ctx, query='保温杯 容量'), C()))
assert items[0].evidence[0].document_id == 'syn-products-a'
assert items[0].evidence[0].source_type == 'local_synthetic'
assert items[0].evidence[0].id.startswith('chunk-')
"""
        env = {**os.environ, "PYTHONPATH": str(root / "python"),
               "SYNTHETIC_MANIFEST": str(root / "data/synthetic/ecommerce-demo-v1/manifest.yaml")}
        completed = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
