"""Provider result types independent of vendor response envelopes."""
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SparseWeight:
    token_id: int
    weight: float


@dataclass(frozen=True)
class EmbeddingResult:
    dense: tuple[float, ...]
    sparse: tuple[SparseWeight, ...]
    model: str
    dimensions: int
    request_id: str
    usage_tokens: int | None


class EmbeddingProvider(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        text_type: Literal["query", "document"],
        dimensions: int = 1024,
        output_type: Literal["dense", "sparse", "dense&sparse"] = "dense&sparse",
        instruct_version: str = "",
    ) -> Sequence[EmbeddingResult]: ...
