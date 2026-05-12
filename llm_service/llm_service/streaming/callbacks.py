"""Streaming callback protocols."""

from __future__ import annotations

from typing import Any, Protocol

from llm_service.core.models import StreamChunk


class OnTokenCallback(Protocol):
    async def __call__(self, chunk: StreamChunk) -> Any: ...


class OnCompleteCallback(Protocol):
    async def __call__(self, full_text: str) -> Any: ...
