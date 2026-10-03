"""The document intelligence module's own thread pool, for its long blocking calls.

Microsoft Graph requests (with their Retry-After waits of up to a minute each), streamed file
downloads and the wait for an extraction child can each take minutes. On asyncio's default
executor they would compete for threads with the rest of AIVA, chat's knowledge search
included (``backend.services.rag.search_knowledge`` runs there, and that pool has only
``min(32, cpu_count + 4)`` threads). A burst of connection tests, or health probes lingering
while Graph throttles, could then delay chat answers (review finding F26). They run here
instead: when this pool is busy only doc-intel work waits.

``run_blocking`` behaves like ``asyncio.to_thread`` (context variables are copied; cancelling
the awaiting task does not stop a call that already started).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")

# One sync run, the health probes of a few sources and a few connection tests at a time.
MAX_WORKERS = 8
THREAD_NAME_PREFIX = "doc-intel"

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def executor() -> ThreadPoolExecutor:
    """The module's pool (created on first use; its threads end with the process)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix=THREAD_NAME_PREFIX)
        return _pool


async def run_blocking(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """``asyncio.to_thread(fn, *args, **kwargs)``, on the doc-intel pool."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(executor(), call)
