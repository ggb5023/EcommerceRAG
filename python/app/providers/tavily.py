"""Tavily search adapter with a redacted, evidence-only response contract."""
from __future__ import annotations

import asyncio
import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import ProviderError, ProviderUsage


@dataclass(frozen=True)
class TavilyResult:
    url: str
    title: str
    content: str
    score: float | None
    published_date: str | None


@dataclass(frozen=True)
class TavilyResponse:
    results: tuple[TavilyResult, ...]
    request_id: str | None
    usage: ProviderUsage | None
    status: str


@dataclass(frozen=True)
class TavilyConfig:
    endpoint: str
    api_key: str
    timeout_s: float
    max_results: int


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes,
        timeout_s: float, cancel_event: Any | None = None,
    ) -> HTTPResponse: ...


def _cancelled(event: Any | None) -> bool:
    checker = getattr(event, "is_set", None)
    return bool(checker and checker())


class UrllibTransport:
    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      body: bytes, timeout_s: float, cancel_event: Any | None = None) -> HTTPResponse:
        if _cancelled(cancel_event):
            raise ProviderError("cancelled", "search request cancelled", provider="tavily")

        def do_request() -> HTTPResponse:
            request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    return HTTPResponse(response.status, dict(response.headers.items()), response.read())
            except urllib.error.HTTPError as error:
                return HTTPResponse(error.code, dict(error.headers.items()), error.read())
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ProviderError("timeout", "search provider unavailable", provider="tavily", retryable=True) from error

        response = await asyncio.to_thread(do_request)
        if _cancelled(cancel_event):
            raise ProviderError("cancelled", "search request cancelled", provider="tavily")
        return response


def load_tavily_config(path: str = "/etc/ecommerce-rag/providers.env") -> TavilyConfig:
    from .config import ProviderConfigError, parse_env_file
    values = parse_env_file(path)
    api_key = values.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise ProviderConfigError("missing provider configuration: TAVILY_API_KEY")
    endpoint = values.get("TAVILY_ENDPOINT", "https://api.tavily.com/search").strip()
    timeout = values.get("TAVILY_TIMEOUT_S", values.get("WEB_SEARCH_TIMEOUT_S", "5")).strip()
    max_results = values.get("TAVILY_MAX_RESULTS", "5").strip()
    try:
        timeout_s = float(timeout)
        limit = int(max_results)
    except ValueError as error:
        raise ProviderConfigError("Tavily timeout or max_results is invalid", code="CONFIG_FAIL") from error
    if timeout_s <= 0 or not 1 <= limit <= 10:
        raise ProviderConfigError("Tavily timeout/max_results is out of range", code="CONFIG_FAIL")
    if not endpoint.startswith(("https://", "http://")):
        raise ProviderConfigError("Tavily endpoint must be an http(s) URL", code="CONFIG_FAIL")
    return TavilyConfig(endpoint, api_key, timeout_s, limit)


def _usage(payload: Mapping[str, Any]) -> ProviderUsage | None:
    value = payload.get("usage")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProviderError("invalid_response", "Tavily usage has an invalid shape", provider="tavily")
    total = value.get("credits", value.get("total_tokens"))
    if total is not None and (not isinstance(total, (int, float)) or not math.isfinite(float(total)) or total < 0):
        raise ProviderError("invalid_response", "Tavily usage is invalid", provider="tavily")
    return ProviderUsage(total_tokens=int(total) if total is not None else None)


class TavilyProvider:
    def __init__(self, config: TavilyConfig, transport: HTTPTransport | None = None):
        self.config = config
        self.transport = transport or UrllibTransport()

    async def search(self, query: str, *, include_domains: Sequence[str] = (),
                     cancel_event: Any | None = None) -> TavilyResponse:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ProviderError("invalid_input", "search query is invalid", provider="tavily")
        body = json.dumps({
            "query": query.strip(), "search_depth": "basic",
            "max_results": self.config.max_results, "topic": "general",
            "include_answer": False, "include_raw_content": False,
            "include_images": False, "auto_parameters": False,
            "include_usage": True, "include_domains": list(include_domains),
        }, separators=(",", ":")).encode()
        response = await self.transport.request(
            "POST", self.config.endpoint,
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            body=body, timeout_s=self.config.timeout_s, cancel_event=cancel_event,
        )
        if response.status == 429:
            raise ProviderError("rate_limited", "Tavily quota exceeded", provider="tavily", status=429, retryable=True)
        if response.status >= 500:
            raise ProviderError("upstream_error", "Tavily service failed", provider="tavily", status=response.status, retryable=True)
        if response.status in {401, 403}:
            raise ProviderError("authentication", "Tavily authentication failed", provider="tavily", status=response.status)
        if response.status < 200 or response.status >= 300:
            raise ProviderError("http_error", "Tavily rejected the request", provider="tavily", status=response.status)
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderError("invalid_response", "Tavily returned invalid JSON", provider="tavily") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise ProviderError("invalid_response", "Tavily results are missing", provider="tavily")
        results: list[TavilyResult] = []
        for item in payload["results"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("url"), str) or not isinstance(item.get("title"), str):
                raise ProviderError("invalid_response", "Tavily result shape is invalid", provider="tavily")
            score = item.get("score")
            if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(float(score))):
                raise ProviderError("invalid_response", "Tavily result score is invalid", provider="tavily")
            content = item.get("content", "")
            if not isinstance(content, str):
                raise ProviderError("invalid_response", "Tavily result content is invalid", provider="tavily")
            results.append(TavilyResult(item["url"], item["title"], content, float(score) if score is not None else None, item.get("published_date")))
        header_id = next((v for k, v in response.headers.items() if k.lower() in {"x-request-id", "request-id"}), None)
        request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else header_id
        return TavilyResponse(tuple(results), request_id, _usage(payload), "success_usable" if results else "success_empty")
