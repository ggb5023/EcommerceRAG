#!/usr/bin/env python3
"""Deterministic M1 RAG gRPC implementation.

The service intentionally uses an in-process fixture.  It is useful for
contract/integration development while keeping provider calls and customer
data completely out of the first milestone.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import grpc

from rag.v1 import rag_pb2, rag_pb2_grpc


def _load_fixture() -> dict[str, str]:
    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "m1_shipping.md").read_bytes()
    _, metadata, content = raw.decode("utf-8").split("---", 2)
    fields = dict(line.split(":", 1) for line in metadata.strip().splitlines())
    content = content.strip()
    digest = sha256(raw).hexdigest()
    return {
        "id": "ev-" + digest[:16], "tenant_id": "m1-tenant", "content": content,
        "source_type": "markdown_fixture", "source_ref": "fixture://m1/shipping",
        "document_id": fields["document_id"].strip(), "title": fields["title"].strip(),
        "version_id": "m1-" + digest[:16], "shop_id": fields["shop_id"].strip(),
        "disclosure_class": fields["disclosure_class"].strip(),
    }


FIXTURES = (_load_fixture(),)


def _allowed(ctx: Any, fixture: dict[str, str]) -> bool:
    shops = set(ctx.allowed_shop_ids)
    if not ctx.tenant_id or fixture["tenant_id"] != ctx.tenant_id:
        return False
    if ctx.shop_id and fixture["shop_id"] != ctx.shop_id:
        return False
    return bool(ctx.all_shops or fixture["shop_id"] in shops)


class RagService(rag_pb2_grpc.RagServiceServicer):
    """M1 deterministic service; Go remains the authorization authority."""

    def Understand(self, request, context):
        query = request.query.strip()
        if not request.context.tenant_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "tenant context is required")
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")
        normalized = "".join(query.lower().split())
        vague_queries = {"这个", "那个", "这个呢", "那个呢", "它呢", "还有吗", "怎么办", "如何处理"}
        if normalized in vague_queries or len(query) <= 2:
            return rag_pb2.UnderstandResponse(
                rewritten_query=query, intent="clarification", information_source="knowledge",
                clarification="请补充你指的商品或具体问题，我再查找对应的帮助信息。",
                confidence=0.2, reason="m1_missing_subject",
            )
        lowered = query.lower()
        intent = "knowledge"
        source = "knowledge"
        if any(word in lowered for word in ("价格", "库存", "订单", "price", "stock", "order")):
            intent, source = "query", "facts"
        return rag_pb2.UnderstandResponse(
            rewritten_query=query, intent=intent, information_source=source,
            clarification="" if query else "请提供问题",
            confidence=0.95,
            reason="deterministic_m1_router",
        )

    def Search(self, request, context):
        if not request.context.tenant_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "tenant context is required")
        query = request.query.strip()
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")
        matches = [f for f in FIXTURES if _allowed(request.context, f)]
        for index, fixture in enumerate(matches, 1):
            yield rag_pb2.SearchResponse(
                phase="retrieval", complete=False,
                evidence=[rag_pb2.Evidence(
                    id=fixture["id"], content=fixture["content"], source_type=fixture["source_type"],
                    source_ref=fixture["source_ref"], document_id=fixture["document_id"],
                    version_id=fixture["version_id"], rank=index, raw_score=1.0 / index,
                    shop_id=fixture["shop_id"], disclosure_class=fixture["disclosure_class"],
                    customer_eligible=True,
                )],
                request_id=request.context.request_id, is_mock=True,
            )
            if not context.is_active():
                return
        yield rag_pb2.SearchResponse(phase="complete", complete=True,
                                     request_id=request.context.request_id, is_mock=True)

    def Generate(self, request, context):
        if not request.context.tenant_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "tenant context is required")
        if hasattr(context, "is_active") and not context.is_active():
            return
        evidence = [
            item for item in request.evidence
            if item.source_type not in ("internal", "unclassified")
            and item.disclosure_class == "external_allowed"
            and item.customer_eligible
            and _allowed(request.context, {"tenant_id": request.context.tenant_id,
                                           "shop_id": item.shop_id})
        ]
        if not evidence:
            text = "暂时没有可引用的知识资料，请补充问题或联系人工客服。"
        else:
            text = "根据当前知识资料：" + "；".join(e.content for e in evidence)
        yield rag_pb2.GenerateResponse(sequence=1, delta=text)
        for index, item in enumerate(evidence, 2):
            yield rag_pb2.GenerateResponse(sequence=index, citation=item)
        yield rag_pb2.GenerateResponse(sequence=len(evidence) + 2, usage_json='{"is_mock":true}')
        yield rag_pb2.GenerateResponse(sequence=len(evidence) + 3, is_mock=True)
        yield rag_pb2.GenerateResponse(sequence=len(evidence) + 4, can_copy=False)
        yield rag_pb2.GenerateResponse(sequence=len(evidence) + 5, done=True)


def register(server: Any) -> None:
    rag_pb2_grpc.add_RagServiceServicer_to_server(RagService(), server)

