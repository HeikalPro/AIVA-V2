from __future__ import annotations

from typing import Protocol, runtime_checkable

from embedding_service.embedders.result import EmbedBatchResult


@runtime_checkable
class Embedder(Protocol):
    """Produces float32 vectors of fixed dimension for a batch of texts."""

    dimension: int

    def embed(self, texts: list[str], conn) -> EmbedBatchResult:
        """Return vectors (same order as inputs) plus optional token usage metadata.

        ``conn`` may be ``None`` for HTTP-backed embedders that do not touch the database.
        """
        ...
