"""Alibaba Bailian (DashScope) adapter and deterministic offline provider.

The adapter uses the documented DashScope REST envelopes and exposes only the
internal provider contracts.  A transport is injected in tests, so response
parsing, validation, timeout and cancellation can be tested without network
access or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import ProviderConfig
from .contracts import (
    ControlResult,
    EmbeddingResult,
    GenerationResult,
    ProviderError,
    ProviderUsage,
    RerankItem,
    RerankResult,
    SparseWeight,
)

ALLOWED_FINISH_REASONS = frozenset(
    {"stop", "length", "tool_calls", "content_filter", "end_turn", "eos", "completed"}
)


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
        cancel_event: Any | None = None,
    ) -> HTTPResponse: ...


def _cancelled(event: Any | None) -> bool:
    if event is None:
        return False
    checker = getattr(event, "is_set", None)
    return bool(checker and checker())


class UrllibTransport:
    """Small standard-library transport; no vendor SDK is needed at runtime."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
        cancel_event: Any | None = None,
    ) -> HTTPResponse:
        if _cancelled(cancel_event):
            raise ProviderError(
                "cancelled", "provider request cancelled", retryable=False
            )

        def do_request() -> HTTPResponse:
            request = urllib.request.Request(
                url, data=body, headers=dict(headers), method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    return HTTPResponse(
                        response.status, dict(response.headers.items()), response.read()
                    )
            except urllib.error.HTTPError as error:
                return HTTPResponse(
                    error.code, dict(error.headers.items()), error.read()
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ProviderError(
                    "timeout",
                    "provider request timed out or was unreachable",
                    retryable=True,
                ) from error

        result = await asyncio.to_thread(do_request)
        if _cancelled(cancel_event):
            raise ProviderError(
                "cancelled", "provider request cancelled", retryable=False
            )
        return result


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _usage(value: object, slot: str = "unknown") -> ProviderUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProviderError("invalid_response", "provider usage has an invalid shape", slot=slot)
    input_tokens = value.get("input_tokens", value.get("prompt_tokens"))
    output_tokens = value.get("output_tokens", value.get("completion_tokens"))
    total_tokens = value.get("total_tokens", value.get("tokens"))
    if (
        total_tokens is None
        and isinstance(input_tokens, int)
        and isinstance(output_tokens, int)
    ):
        total_tokens = input_tokens + output_tokens
    for name, number in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        if number is not None and (
            not isinstance(number, int) or isinstance(number, bool) or number < 0
        ):
            raise ProviderError("invalid_response", f"provider usage {name} is invalid", slot=slot)
    return ProviderUsage(input_tokens, output_tokens, total_tokens)


def _request_id(response: HTTPResponse, payload: Mapping[str, Any]) -> str:
    value = payload.get("request_id") or payload.get("id")
    if not value:
        value = next(
            (
                v
                for k, v in response.headers.items()
                if k.lower() in {"x-request-id", "request-id"}
            ),
            None,
        )
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(
            "invalid_response", "provider response did not include a request id"
        )
    return value.strip()


def _json(response: HTTPResponse, slot: str) -> Mapping[str, Any]:
    if response.status == 429:
        raise ProviderError(
            "rate_limited",
            "provider quota was exceeded",
            slot=slot,
            status=429,
            retryable=True,
        )
    if response.status == 408 or response.status == 504:
        raise ProviderError(
            "timeout",
            "provider request timed out",
            slot=slot,
            status=response.status,
            retryable=True,
        )
    if 500 <= response.status <= 599:
        raise ProviderError(
            "upstream_error",
            "provider service failed",
            slot=slot,
            status=response.status,
            retryable=True,
        )
    if response.status in {401, 403}:
        raise ProviderError(
            "authentication",
            "provider authentication failed",
            slot=slot,
            status=response.status,
        )
    if response.status < 200 or response.status >= 300:
        raise ProviderError(
            "http_error",
            "provider rejected the request",
            slot=slot,
            status=response.status,
        )
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            "invalid_response",
            "provider returned invalid JSON",
            slot=slot,
            status=response.status,
        ) from error
    if not isinstance(payload, Mapping):
        raise ProviderError(
            "invalid_response",
            "provider response must be an object",
            slot=slot,
            status=response.status,
        )
    if payload.get("code") and not payload.get("output") and not payload.get("choices"):
        raise ProviderError(
            "vendor_error",
            "provider returned an error response",
            slot=slot,
            status=response.status,
        )
    return payload


def _messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    if not messages:
        raise ProviderError("invalid_input", "messages must not be empty")
    result: list[dict[str, str]] = []
    for message in messages:
        if (
            not isinstance(message, Mapping)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
        ):
            raise ProviderError(
                "invalid_input", "each message requires role and content"
            )
        result.append({"role": message["role"], "content": message["content"]})
    return result


def _content(payload: Mapping[str, Any], slot: str) -> tuple[str, str]:
    choices = payload.get("choices")
    output = payload.get("output")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice = choices[0]
        message = choice.get("message")
        content = (
            message.get("content")
            if isinstance(message, Mapping)
            else choice.get("text")
        )
        finish = choice.get("finish_reason")
    elif isinstance(output, Mapping):
        choices = output.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            choice = choices[0]
            message = choice.get("message")
            content = (
                message.get("content")
                if isinstance(message, Mapping)
                else choice.get("text")
            )
            finish = choice.get("finish_reason")
        else:
            content = output.get("text")
            finish = output.get("finish_reason")
    else:
        content, finish = None, None
    if not isinstance(content, str):
        raise ProviderError("invalid_response", f"{slot} response did not include text")
    if not isinstance(finish, str) or finish not in ALLOWED_FINISH_REASONS:
        raise ProviderError(
            "invalid_response", f"{slot} response has an invalid finish reason"
        )
    return content, finish


def _schema_matches(value: object, schema: Mapping[str, Any]) -> bool:
    """Validate the small JSON-schema subset used at the provider boundary."""

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(key, str) or key not in value for key in required
        ):
            return False
        properties = schema.get("properties", {})
        if properties and not isinstance(properties, Mapping):
            return False
        return all(
            isinstance(key, str)
            and isinstance(rule, Mapping)
            and _schema_matches(value[key], rule)
            for key, rule in properties.items()
            if key in value
        )
    if expected_type == "array":
        if not isinstance(value, list):
            return False
        item_schema = schema.get("items")
        return not isinstance(item_schema, Mapping) or all(
            _schema_matches(item, item_schema) for item in value
        )
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return _finite(value)
    if expected_type == "boolean":
        return isinstance(value, bool)
    return True


def _structured(
    content: str, slot: str, schema: Mapping[str, Any] | None = None
) -> Mapping[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            "schema_error", f"{slot} structured output is not valid JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise ProviderError(
            "schema_error", f"{slot} structured output must be an object"
        )
    actual_schema = None
    if isinstance(schema, Mapping):
        actual_schema = schema.get("schema", schema)
    if isinstance(actual_schema, Mapping) and not _schema_matches(value, actual_schema):
        raise ProviderError(
            "schema_error", f"{slot} structured output does not match schema"
        )
    return dict(value)


class AlibabaBailianProvider:
    """All four Bailian slots behind one explicitly selected profile."""

    provider_name = "aliyun-bailian"

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if config.profile != "aliyun-bailian":
            raise ValueError(
                "AlibabaBailianProvider requires the aliyun-bailian profile"
            )
        self.config = config
        self.transport = transport or UrllibTransport()

    def _url(self, path: str) -> str:
        return self.config.endpoint.rstrip("/") + "/" + path.lstrip("/")

    async def _call(
        self,
        slot: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None,
        cancel_event: Any | None,
    ) -> tuple[Mapping[str, Any], HTTPResponse]:
        if _cancelled(cancel_event):
            raise ProviderError("cancelled", "provider request cancelled", slot=slot)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-WorkSpace": self.config.workspace_id or "",
            "X-Ecommerce-RAG-Provider": self.provider_name,
        }
        # Empty optional workspace headers are omitted; the key itself never
        # appears in error messages, logs or result objects.
        headers = {key: value for key, value in headers.items() if value}
        try:
            response = await self.transport.request(
                "POST",
                self._url(self._path(slot)),
                headers=headers,
                body=body,
                timeout_s=timeout_s or self.config.timeout_s,
                cancel_event=cancel_event,
            )
        except ProviderError as error:
            messages = {
                "cancelled": "provider request cancelled",
                "timeout": "provider request timed out",
                "rate_limited": "provider quota was exceeded",
                "upstream_error": "provider service failed",
            }
            raise ProviderError(
                error.code,
                messages.get(error.code, "provider request failed"),
                provider=self.provider_name,
                slot=slot,
                retryable=error.retryable,
                status=error.status,
                request_id=error.request_id,
            ) from error
        except TimeoutError as error:
            raise ProviderError(
                "timeout", "provider request timed out", slot=slot, retryable=True
            ) from error
        return _json(response, slot), response

    @staticmethod
    def _path(slot: str) -> str:
        if slot in {"control", "generation"}:
            return "compatible-mode/v1/chat/completions"
        if slot == "embedding":
            return "api/v1/services/embeddings/text-embedding/text-embedding"
        return "api/v1/services/rerank/text-rerank/text-rerank"

    async def control(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> ControlResult:
        payload: dict[str, Any] = {
            "model": self.config.control_model,
            "messages": _messages(messages),
            "response_format": {"type": "json_object"},
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": dict(response_schema),
            }
        data, response = await self._call(
            "control", payload, timeout_s=timeout_s, cancel_event=cancel_event
        )
        content, finish = _content(data, "control")
        return ControlResult(
            _structured(content, "control", response_schema),
            self.config.control_model,
            _request_id(response, data),
            finish,
            _usage(data.get("usage"), "control"),
        )

    async def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> GenerationResult:
        payload: dict[str, Any] = {
            "model": self.config.generation_model,
            "messages": _messages(messages),
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": dict(response_schema),
            }
        data, response = await self._call(
            "generation", payload, timeout_s=timeout_s, cancel_event=cancel_event
        )
        content, finish = _content(data, "generation")
        structured = (
            _structured(content, "generation", response_schema)
            if response_schema is not None
            else None
        )
        return GenerationResult(
            content,
            structured,
            self.config.generation_model,
            _request_id(response, data),
            finish,
            _usage(data.get("usage"), "generation"),
        )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        dimensions: int = 1024,
        output_type: str = "dense&sparse",
        instruct_version: str = "",
        cancel_event: Any | None = None,
    ) -> Sequence[EmbeddingResult]:
        if dimensions != 1024:
            raise ProviderError(
                "invalid_input", "embedding dimensions must be 1024", slot="embedding"
            )
        if text_type not in {"query", "document"}:
            raise ProviderError(
                "invalid_input", "embedding text_type is invalid", slot="embedding"
            )
        if (
            output_type not in {"dense", "sparse", "dense&sparse"}
            or not texts
            or any(not isinstance(text, str) for text in texts)
        ):
            raise ProviderError(
                "invalid_input", "embedding input is invalid", slot="embedding"
            )
        parameters: dict[str, Any] = {
            "text_type": text_type,
            "dimension": dimensions,
            "output_type": output_type,
        }
        if instruct_version:
            parameters["instruct"] = instruct_version
        payload = {
            "model": self.config.embedding_model,
            "input": {"texts": list(texts)},
            "parameters": parameters,
        }
        data, response = await self._call(
            "embedding", payload, timeout_s=None, cancel_event=cancel_event
        )
        output = data.get("output")
        rows = (
            output.get("embeddings")
            if isinstance(output, Mapping)
            else data.get("embeddings")
        )
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ProviderError(
                "invalid_response",
                "embedding output count does not match input",
                slot="embedding",
            )
        request_id = _request_id(response, data)
        usage = _usage(data.get("usage"), "embedding")
        result: list[EmbeddingResult] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ProviderError(
                    "invalid_response", "embedding item is invalid", slot="embedding"
                )
            dense_value = row.get("embedding", row.get("dense"))
            if isinstance(dense_value, Mapping):
                dense_value = dense_value.get("dense")
            if isinstance(dense_value, list) and all(
                _finite(value) for value in dense_value
            ):
                dense = tuple(float(value) for value in dense_value)
            else:
                dense = ()
            if output_type in {"dense", "dense&sparse"} and len(dense) != 1024:
                raise ProviderError(
                    "invalid_response",
                    "embedding dense vector must contain 1024 finite values",
                    slot="embedding",
                )
            sparse_value = row.get("sparse_embedding", row.get("sparse", ()))
            if isinstance(sparse_value, Mapping):
                sparse_value = sparse_value.get("values", sparse_value.get("items", ()))
            if sparse_value is None:
                sparse_value = ()
            if not isinstance(sparse_value, list):
                raise ProviderError(
                    "invalid_response",
                    "embedding sparse vector is invalid",
                    slot="embedding",
                )
            sparse: list[SparseWeight] = []
            seen: set[int] = set()
            for item in sparse_value:
                if not isinstance(item, Mapping):
                    raise ProviderError(
                        "invalid_response",
                        "embedding sparse item is invalid",
                        slot="embedding",
                    )
                token_id = item.get("token_id", item.get("index"))
                weight = item.get("weight", item.get("value"))
                if (
                    not isinstance(token_id, int)
                    or isinstance(token_id, bool)
                    or token_id < 0
                    or token_id in seen
                    or not _finite(weight)
                ):
                    raise ProviderError(
                        "invalid_response",
                        "embedding sparse item is invalid",
                        slot="embedding",
                    )
                seen.add(token_id)
                sparse.append(SparseWeight(token_id, float(weight)))
            if output_type in {"sparse", "dense&sparse"} and not sparse:
                raise ProviderError(
                    "invalid_response",
                    "embedding sparse output is missing",
                    slot="embedding",
                )
            result.append(
                EmbeddingResult(
                    dense,
                    tuple(sparse),
                    self.config.embedding_model,
                    1024,
                    request_id,
                    usage.total_tokens if usage else None,
                    usage,
                )
            )
        return result

    async def rerank(
        self,
        query: str,
        candidates: Sequence[str],
        *,
        top_n: int | None = None,
        instruct: str = "",
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> RerankResult:
        if (
            not isinstance(query, str)
            or not query.strip()
            or not candidates
            or any(not isinstance(item, str) for item in candidates)
        ):
            raise ProviderError(
                "invalid_input",
                "rerank query and candidates are required",
                slot="rerank",
            )
        if top_n is not None and (
            not isinstance(top_n, int) or top_n <= 0 or top_n > len(candidates)
        ):
            raise ProviderError(
                "invalid_input",
                "rerank top_n must be within candidate count",
                slot="rerank",
            )
        parameters: dict[str, Any] = {"return_documents": False}
        if top_n is not None:
            parameters["top_n"] = top_n
        if instruct:
            parameters["instruct"] = instruct
        payload = {
            "model": self.config.rerank_model,
            "input": {"query": query, "documents": list(candidates)},
            "parameters": parameters,
        }
        data, response = await self._call(
            "rerank", payload, timeout_s=timeout_s, cancel_event=cancel_event
        )
        output = data.get("output")
        rows = (
            output.get("results")
            if isinstance(output, Mapping)
            else data.get("results")
        )
        if not isinstance(rows, list):
            raise ProviderError(
                "invalid_response", "rerank results are missing", slot="rerank"
            )
        expected = top_n or len(candidates)
        if len(rows) != expected:
            raise ProviderError(
                "invalid_response", "rerank result count is incomplete", slot="rerank"
            )
        items: list[RerankItem] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ProviderError(
                    "invalid_response", "rerank item is invalid", slot="rerank"
                )
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if (
                not isinstance(index, int)
                or index < 0
                or index >= len(candidates)
                or index in seen
                or not _finite(score)
                or not 0 <= float(score) <= 1
            ):
                raise ProviderError(
                    "invalid_response",
                    "rerank index or score is invalid",
                    slot="rerank",
                )
            seen.add(index)
            items.append(RerankItem(index, float(score)))
        return RerankResult(
            tuple(items),
            self.config.rerank_model,
            _request_id(response, data),
            _usage(data.get("usage"), "rerank"),
        )


class MockProvider:
    """Deterministic provider for M1 and offline contract tests."""

    provider_name = "mock"
    is_mock = True

    def __init__(self, config: ProviderConfig) -> None:
        if config.profile != "mock":
            raise ValueError("MockProvider requires the mock profile")
        self.config = config

    @staticmethod
    def _id(slot: str, value: str) -> str:
        return f"mock-{slot}-" + hashlib.sha256(value.encode()).hexdigest()[:16]

    async def control(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> ControlResult:
        _ = timeout_s
        if _cancelled(cancel_event):
            raise ProviderError(
                "cancelled",
                "provider request cancelled",
                provider="mock",
                slot="control",
            )
        checked = _messages(messages)
        query = checked[-1]["content"]
        structured = {
            "intent": "knowledge",
            "information_source": "knowledge",
            "rewritten_query": query,
        }
        return ControlResult(
            structured,
            self.config.control_model,
            self._id("control", query),
            "stop",
            None,
        )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        text_type: str,
        dimensions: int = 1024,
        output_type: str = "dense&sparse",
        instruct_version: str = "",
        cancel_event: Any | None = None,
    ) -> Sequence[EmbeddingResult]:
        if dimensions != 1024:
            raise ProviderError(
                "invalid_input",
                "embedding dimensions must be 1024",
                provider="mock",
                slot="embedding",
            )
        if (
            text_type not in {"query", "document"}
            or output_type not in {"dense", "sparse", "dense&sparse"}
            or not texts
            or any(not isinstance(text, str) for text in texts)
        ):
            raise ProviderError(
                "invalid_input",
                "embedding input is invalid",
                provider="mock",
                slot="embedding",
            )
        results: list[EmbeddingResult] = []
        for text in texts:
            if _cancelled(cancel_event):
                raise ProviderError(
                    "cancelled",
                    "provider request cancelled",
                    provider="mock",
                    slot="embedding",
                )
            digest = hashlib.sha256((text + "\0" + instruct_version).encode()).digest()
            dense = (
                tuple(
                    ((digest[index % len(digest)] / 255.0) * 2) - 1
                    for index in range(1024)
                )
                if output_type != "sparse"
                else ()
            )
            sparse = (
                tuple(
                    SparseWeight(index, digest[index] / 255.0)
                    for index in range(0, min(8, len(digest)), 2)
                )
                if output_type != "dense"
                else ()
            )
            request_id = self._id("embedding", text)
            results.append(
                EmbeddingResult(
                    dense,
                    sparse,
                    self.config.embedding_model,
                    1024,
                    request_id,
                    None,
                    None,
                )
            )
        return results

    async def rerank(
        self,
        query: str,
        candidates: Sequence[str],
        *,
        top_n: int | None = None,
        instruct: str = "",
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> RerankResult:
        _ = instruct, timeout_s
        if (
            not isinstance(query, str)
            or not query.strip()
            or not candidates
            or any(not isinstance(candidate, str) for candidate in candidates)
        ):
            raise ProviderError(
                "invalid_input",
                "rerank query and candidates are required",
                provider="mock",
                slot="rerank",
            )
        if top_n is not None and (
            not isinstance(top_n, int)
            or isinstance(top_n, bool)
            or top_n <= 0
            or top_n > len(candidates)
        ):
            raise ProviderError(
                "invalid_input",
                "rerank top_n must be within candidate count",
                provider="mock",
                slot="rerank",
            )
        if _cancelled(cancel_event):
            raise ProviderError(
                "cancelled",
                "provider request cancelled",
                provider="mock",
                slot="rerank",
            )
        query_words = set(query.lower().split())
        scored = sorted(
            (
                (
                    len(query_words & set(candidate.lower().split()))
                    / max(1, len(query_words)),
                    index,
                )
                for index, candidate in enumerate(candidates)
            ),
            reverse=True,
        )
        chosen = scored[:top_n] if top_n else scored
        return RerankResult(
            tuple(RerankItem(index, score) for score, index in chosen),
            self.config.rerank_model,
            self._id("rerank", query + "\0" + "\0".join(candidates)),
            None,
        )

    async def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> GenerationResult:
        _ = timeout_s
        checked = _messages(messages)
        if _cancelled(cancel_event):
            raise ProviderError(
                "cancelled",
                "provider request cancelled",
                provider="mock",
                slot="generation",
            )
        text = checked[-1]["content"]
        structured = {"answer": text} if response_schema is not None else None
        return GenerationResult(
            text,
            structured,
            self.config.generation_model,
            self._id("generation", text),
            "stop",
            None,
        )


def build_provider(config: ProviderConfig) -> AlibabaBailianProvider | MockProvider:
    """Build exactly the selected profile; failures never auto-switch models."""

    if config.profile == "mock":
        return MockProvider(config)
    if config.profile == "aliyun-bailian":
        return AlibabaBailianProvider(config)
    raise ProviderError("config_blocked", "unknown provider profile")
