#!/usr/bin/env python3
"""Deterministic M1 RAG gRPC implementation.

The service intentionally uses an in-process fixture.  It is useful for
contract/integration development while keeping provider calls and customer
data completely out of the first milestone.
"""
from __future__ import annotations

import os
import re
import time
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import grpc

from app.authorization import ScopeError, validate_scope
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
_PROFILE = os.environ.get("RAG_PROFILE") or ("synthetic_import_mock" if os.environ.get("SYNTHETIC_MANIFEST") else "m1_fixture_mock")
if _PROFILE not in {"m1_fixture_mock", "synthetic_import_mock"} or (_PROFILE == "synthetic_import_mock") != bool(os.environ.get("SYNTHETIC_MANIFEST")):
    raise ValueError("RAG_PROFILE and manifest must select a consistent mock profile")
_BUSINESS_DATE = os.environ.get("SYNTHETIC_BUSINESS_DATE") or datetime.now(UTC).date().isoformat()
date.fromisoformat(_BUSINESS_DATE)
_PACING_MS = int(os.environ.get("MOCK_STREAM_PACING_MS", "0"))
if not 0 <= _PACING_MS <= 100:
    raise ValueError("MOCK_STREAM_PACING_MS must be between 0 and 100")
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


def _valid_scope(ctx: Any) -> bool:
    shops = set(ctx.allowed_shop_ids)
    if not ctx.request_id or not ctx.tenant_id or not ctx.user_id or not ctx.permission_revision or not ctx.enforce_document_scope:
        return False
    if not shops or ctx.role not in {"owner", "admin", "operator", "viewer"}:
        return False
    if ctx.shop_id and ctx.shop_id not in shops:
        return False
    if ctx.all_shops:
        return ctx.role in {"owner", "admin"}
    return True


def _allowed(ctx: Any, fixture: dict[str, str]) -> bool:
    if not _valid_scope(ctx) or fixture["tenant_id"] != ctx.tenant_id:
        return False
    if fixture["shop_id"] not in set(ctx.allowed_shop_ids):
        return False
    if fixture.get("document_id") not in set(ctx.allowed_document_ids):
        return False
    return not ctx.shop_id or fixture["shop_id"] == ctx.shop_id


class RagService(rag_pb2_grpc.RagServiceServicer):
    """M1 deterministic service; Go remains the authorization authority."""

    def __init__(self, scope_validator=None):
        self._scope_validator = scope_validator or validate_scope

    def _authorize(self, scope, context):
        if not _valid_scope(scope):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "authorization scope is invalid")
        try:
            self._scope_validator(scope)
        except ScopeError as error:
            context.abort(error.code, "authorization scope could not be verified")

    def _canonical(self, item):
        if _LOCAL_INDEX is not None:
            return any(c.tenant_id == item.tenant_id and c.shop_id == item.shop_id
                       and c.document_id == item.document_id and c.version_id == item.version_id
                       and c.chunk_id == item.id and c.content == item.content
                       and c.source_ref == item.source_ref
                       and c.disclosure_class == item.disclosure_class
                       and _LOCAL_INDEX._active_versions.get((c.tenant_id, c.document_id)) == c.version_id
                       and (not c.effective_from or _BUSINESS_DATE >= c.effective_from)
                       and (not c.effective_to or _BUSINESS_DATE < c.effective_to)
                       for c in _LOCAL_INDEX.chunks)
        return any(f["tenant_id"] == item.tenant_id and f["shop_id"] == item.shop_id
                   and f["document_id"] == item.document_id and f["version_id"] == item.version_id
                   and f["id"] == item.id and f["content"] == item.content
                   and f["source_ref"] == item.source_ref and f["disclosure_class"] == item.disclosure_class
                   for f in FIXTURES)

    def Understand(self, request, context):
        query = request.query.strip()
        self._authorize(request.context, context)
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")
        if _is_vague(query) or len(query) <= 2:
            if re.search(r"SKU-[A-Z0-9-]+|保温杯|毛巾|收纳箱|马克杯|配送|退货", request.history_summary, re.IGNORECASE):
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
        self._authorize(request.context, context)
        query = request.query.strip()
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")
        if _LOCAL_INDEX is not None:
            fact_query = any(word in query.lower() for word in ("价格", "库存", "订单", "price", "stock", "order"))
            documents = {c.document_id for c in _LOCAL_INDEX.chunks
                         if bool(c.metadata.get("fact_type")) == fact_query}
            matches = _LOCAL_INDEX.search(query, tenant_id=request.context.tenant_id,
                                          shop_id=request.context.shop_id or None,
                                          allowed_shop_ids=set(request.context.allowed_shop_ids),
                                          role=request.context.role,
                                          allowed_document_ids=set(request.context.allowed_document_ids) & documents,
                                          adjacent_window=1, business_date=_BUSINESS_DATE)
            for index, item in enumerate(matches, 1):
                self._authorize(request.context, context)
                yield rag_pb2.SearchResponse(
                    phase="retrieval", complete=False,
                    evidence=[rag_pb2.Evidence(
                        id=item["chunk_id"], content=item["content"], source_type="local_synthetic",
                        tenant_id=item["tenant_id"],
                        source_ref=item["source_ref"], document_id=item["document_id"],
                        version_id=item["version_id"], rank=index, raw_score=item["score"],
                        shop_id=item["shop_id"], disclosure_class=item["disclosure_class"],
                        customer_eligible=item["disclosure_class"] == "external_allowed",
                    )], request_id=request.context.request_id, is_mock=True)
            self._authorize(request.context, context)
            yield rag_pb2.SearchResponse(phase="complete", complete=True,
                                         request_id=request.context.request_id, is_mock=True)
            return
        matches = [f for f in _fixture_matches(query) if _allowed(request.context, f)]
        for index, fixture in enumerate(matches, 1):
            self._authorize(request.context, context)
            yield rag_pb2.SearchResponse(
                phase="retrieval", complete=False,
                evidence=[rag_pb2.Evidence(
                    id=fixture["id"], content=fixture["content"], source_type=fixture["source_type"],
                    tenant_id=fixture["tenant_id"],
                    source_ref=fixture["source_ref"], document_id=fixture["document_id"],
                    version_id=fixture["version_id"], rank=index, raw_score=1.0 / index,
                    shop_id=fixture["shop_id"], disclosure_class=fixture["disclosure_class"],
                    customer_eligible=True,
                )],
                request_id=request.context.request_id, is_mock=True,
            )
            if not context.is_active():
                return
        self._authorize(request.context, context)
        yield rag_pb2.SearchResponse(phase="complete", complete=True,
                                     request_id=request.context.request_id, is_mock=True)

    def Generate(self, request, context):
        self._authorize(request.context, context)
        if hasattr(context, "is_active") and not context.is_active():
            return
        evidence = [
            item for item in request.evidence
            if item.source_type not in ("internal", "unclassified")
            and item.disclosure_class == "external_allowed"
            and item.customer_eligible
            and _allowed(request.context, {"tenant_id": item.tenant_id,
                                           "shop_id": item.shop_id, "document_id": item.document_id})
        ]
        if any(not self._canonical(item) for item in evidence):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "evidence does not match authorized source")
        if not evidence:
            text = "暂时没有可引用的知识资料，请补充问题或联系人工客服。"
        else:
            text = "根据当前知识资料：" + "；".join(e.content for e in evidence)
        chunks = [text] if _PROFILE == "m1_fixture_mock" else [text[i:i + 32] for i in range(0, len(text), 32)]
        sequence = 0
        for chunk in chunks:
            if hasattr(context, "is_active") and not context.is_active():
                return
            if _PACING_MS and _PROFILE == "synthetic_import_mock":
                time.sleep(_PACING_MS / 1000)
            self._authorize(request.context, context)
            if hasattr(context, "is_active") and not context.is_active():
                return
            sequence += 1
            yield rag_pb2.GenerateResponse(sequence=sequence, delta=chunk)
        for item in evidence:
            self._authorize(request.context, context)
            sequence += 1
            yield rag_pb2.GenerateResponse(sequence=sequence, citation=item)
        for event in ({"usage_json": '{"is_mock":true}'}, {"is_mock": True}, {"can_copy": False}, {"done": True}):
            self._authorize(request.context, context)
            sequence += 1
            yield rag_pb2.GenerateResponse(sequence=sequence, **event)


def register(server: Any) -> None:
    rag_pb2_grpc.add_RagServiceServicer_to_server(RagService(), server)
