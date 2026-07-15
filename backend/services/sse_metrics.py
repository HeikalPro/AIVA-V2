"""In-process counter of live Server-Sent-Events (chat streaming) connections.

The count lives in this worker process only; with multiple uvicorn workers each
reports its own streams. Good enough for an ops health snapshot.
"""

from __future__ import annotations

from typing import AsyncIterator, TypeVar

_T = TypeVar("_T")

_active_sse_connections = 0


def active_sse_connections() -> int:
    """Number of SSE streams currently open in this process."""
    return _active_sse_connections


async def count_stream(agen: AsyncIterator[_T]) -> AsyncIterator[_T]:
    """Wrap a streaming generator so it is counted while open.

    Increments on first iteration and decrements in a ``finally`` — so the count
    is released whether the stream finishes normally, errors, or the client
    disconnects (which raises ``GeneratorExit`` into the generator).
    """
    global _active_sse_connections
    _active_sse_connections += 1
    try:
        async for item in agen:
            yield item
    finally:
        _active_sse_connections -= 1
        if _active_sse_connections < 0:
            _active_sse_connections = 0
