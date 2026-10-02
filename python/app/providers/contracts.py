"""Provider contracts independent of vendor response envelopes.

The provider boundary deliberately contains no vendor SDK types.  Adapters
translate a vendor response into these values and raise :class:`ProviderError`
for anything that cannot be trusted.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SparseWeight:
    token_id: int
    weight: float


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized usage; ``None`` means the vendor did not provide it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ProviderError(RuntimeError):
    """Stable, non-sensitive provider failure classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str = "aliyun-bailian",
        slot: str = "unknown",
        retryable: bool = False,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.slot = slot
        self.retryable = retryable
        self.status = status
        self.request_id = request_id


@dataclass(frozen=True)
class ControlResult:
    structured: Mapping[str, Any]
    model: str
    request_id: str
    finish_reason: str
    usage: ProviderUsage | None


@dataclass(frozen=True)
class RerankItem:
    index: int
    score: float


@dataclass(frozen=True)
class RerankResult:
    items: tuple[RerankItem, ...]
    model: str
    request_id: str
    usage: ProviderUsage | None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    structured: Mapping[str, Any] | None
    model: str
    request_id: str
    finish_reason: str
    usage: ProviderUsage | None


@dataclass(frozen=True)
class EmbeddingResult:
    dense: tuple[float, ...]
    sparse: tuple[SparseWeight, ...]
    model: str
    dimensions: int
    request_id: str
    usage_tokens: int | None
    usage: ProviderUsage | None = None


class EmbeddingProvider(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        text_type: Literal["query", "document"],
        dimensions: int = 1024,
        output_type: Literal["dense", "sparse", "dense&sparse"] = "dense&sparse",
        instruct_version: str = "",
        cancel_event: Any | None = None,
    ) -> Sequence[EmbeddingResult]: ...


class ControlProvider(Protocol):
    async def control(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> ControlResult: ...


class RerankProvider(Protocol):
    async def rerank(
        self,
        query: str,
        candidates: Sequence[str],
        *,
        top_n: int | None = None,
        instruct: str = "",
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> RerankResult: ...


class GenerationProvider(Protocol):
    async def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        cancel_event: Any | None = None,
    ) -> GenerationResult: ...
