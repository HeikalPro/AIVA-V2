"""Stream handler with optional callbacks."""

from __future__ import annotations

from collections.abc import AsyncIterator

from llm_service.core.models import StreamChunk
from llm_service.streaming.callbacks import OnCompleteCallback, OnTokenCallback


class StreamHandler:
    def __init__(self, callbacks: list[OnTokenCallback] | None = None) -> None:
        self._callbacks = callbacks or []

    async def handle(
        self,
        stream: AsyncIterator[StreamChunk],
        on_complete: OnCompleteCallback | None = None,
    ) -> AsyncIterator[StreamChunk]:
        parts: list[str] = []
        async for chunk in stream:
            parts.append(chunk.delta)
            for cb in self._callbacks:
                await cb(chunk)
            yield chunk
        if on_complete:
            await on_complete("".join(parts))
