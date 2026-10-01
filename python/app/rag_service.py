#!/usr/bin/env python3
"""Deterministic M1 RAG gRPC implementation.

The service intentionally uses an in-process fixture.  It is useful for
contract/integration development while keeping provider calls and customer
data completely out of the first milestone.
"""
from __future__ import annotations

import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import grpc

from app.ingest.pipeline import load_manifest_index
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


_SHIPPING = _load_fixture()
_SYNTHETIC_FIXTURES = (
    {
        "id": "ev-m1-bottle", "tenant_id": "m1-tenant", "shop_id": "shop-demo",
        "content": "保温杯容量为480毫升，杯口直径为6厘米。本文仅用于 M1 隔离验证。",
        "source_type": "markdown_fixture", "source_ref": "fixture://m1/bottle",
        "document_id": "m1-demo-bottle", "version_id": "m1-bottle-v1",
        "disclosure_class": "external_allowed",
    },
    {
        "id": "ev-m1-care", "tenant_id": "m1-tenant", "shop_id": "shop-demo",
        "content": "竹纤维毛巾建议低温烘干，避免高温损伤纤维。本文仅用于 M1 隔离验证。",
        "source_type": "markdown_fixture", "source_ref": "fixture://m1/care",
        "document_id": "m1-demo-care", "version_id": "m1-care-v1",
        "disclosure_class": "external_allowed",
    },
    {
        "id": "ev-m1-storage", "tenant_id": "m1-tenant", "shop_id": "shop-demo",
        "content": "蓝色收纳箱有低款和高款两个版本，具体尺寸应按型号确认。本文仅用于 M1 隔离验证。",
        "source_type": "markdown_fixture", "source_ref": "fixture://m1/storage",
        "document_id": "m1-demo-storage", "version_id": "m1-storage-v1",
        "disclosure_class": "external_allowed",
    },
    {
        "id": "ev-m1-unanswerable", "tenant_id": "m1-tenant", "shop_id": "shop-demo",
        "content": "现有隔离资料没有覆盖该问题，不能据此作出确定承诺。本文仅用于 M1 隔离验证。",
        "source_type": "markdown_fixture", "source_ref": "fixture://m1/unanswerable",
        "document_id": "m1-demo-unanswerable", "version_id": "m1-unanswerable-v1",
        "disclosure_class": "external_allowed",
    },
)
FIXTURES = (_SHIPPING, *_SYNTHETIC_FIXTURES)
_LOCAL_INDEX = None
if os.environ.get("SYNTHETIC_MANIFEST"):
    _LOCAL_INDEX = load_manifest_index(Path(os.environ["SYNTHETIC_MANIFEST"]))


def _is_vague(query: str) -> bool:
    normalized = re.sub(r"\s+", "", query.lower())
    if normalized in {"还有吗", "怎么办", "如何处理"}:
        return True
    return bool(re.fullmatch(r"(?:这|那|它|这个|那个)(?:个)?(?:怎么样|如何|好吗|行吗|可以吗|呢)?[？?]?'?", normalized))


def _fixture_matches(query: str) -> list[dict[str, str]]:
    lowered = query.lower()
    if any(word in lowered for word in ("配送", "退货", "签收", "工作日")):
        return [_SHIPPING]
    if any(word in lowered for word in ("保温杯", "容量", "杯口")):
        return [FIXTURES[1]]
    if any(word in lowered for word in ("毛巾", "烘干", "羊毛", "洗护")):
        return [FIXTURES[2]]
    if any(word in lowered for word in ("收纳箱", "蓝色", "尺寸", "大小")):
        return [FIXTURES[3]]
    if any(word in lowered for word in ("热油", "保证", "能否", "安全")):
        return [FIXTURES[4]]
    return []


def _allowed(ctx: Any, fixture: dict[str, str]) -> bool:
    shops = set(ctx.allowed_shop_ids)
    if not ctx.tenant_id or not ctx.user_id or not ctx.permission_revision:
        return False
    if fixture["tenant_id"] != ctx.tenant_id:
        return False
    if ctx.shop_id and fixture["shop_id"] != ctx.shop_id:
        return False
    if ctx.all_shops:
        return bool(ctx.role in {"owner", "admin"} and shops)
    return bool(shops and fixture["shop_id"] in shops)


class RagService(rag_pb2_grpc.RagServiceServicer):
    """M1 deterministic service; Go remains the authorization authority."""

    def Understand(self, request, context):
        query = request.query.strip()
        if not request.context.tenant_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "tenant context is required")
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")
        if _is_vague(query) or len(query) <= 2:
            if request.history_summary.strip():
                return rag_pb2.UnderstandResponse(
                    rewritten_query=request.history_summary.strip() + "\n补充问题：" + query,
                    intent="knowledge", information_source="knowledge", confidence=0.7,
                    reason="deterministic_m1_history_context",
                )
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
        if _LOCAL_INDEX is not None:
            matches = _LOCAL_INDEX.search(query, tenant_id=request.context.tenant_id,
                                          shop_id=request.context.shop_id or None,
                                          allowed_shop_ids=set(request.context.allowed_shop_ids),
                                          role=request.context.role)
            for index, item in enumerate(matches, 1):
                yield rag_pb2.SearchResponse(
                    phase="retrieval", complete=False,
                    evidence=[rag_pb2.Evidence(
                        id=item["chunk_id"], content=item["content"], source_type="local_synthetic",
                        source_ref=item["source_ref"], document_id=item["document_id"],
                        version_id=item["version_id"], rank=index, raw_score=item["score"],
                        shop_id=item["shop_id"], disclosure_class=item["disclosure_class"],
                        customer_eligible=item["disclosure_class"] == "external_allowed",
                    )], request_id=request.context.request_id, is_mock=True)
            yield rag_pb2.SearchResponse(phase="complete", complete=True,
                                         request_id=request.context.request_id, is_mock=True)
            return
        matches = [f for f in _fixture_matches(query) if _allowed(request.context, f)]
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
