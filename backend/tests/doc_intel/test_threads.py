"""Review finding F26: the long blocking calls of SharePoint sync (Microsoft Graph requests with
their Retry-After waits, downloads, the wait for an extraction child) and of the Microsoft
health probe run on doc-intel's own thread pool, never on asyncio's default executor, which
chat's knowledge search (backend.services.rag.search_knowledge) shares with the rest of AIVA."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.doc_intel import health, threads
from backend.doc_intel.health import run_checks
from backend.doc_intel.schemas import SourceCreate

from ._crm_fakes import CrmEnv, crm_env, remote, source_body  # noqa: F401  (crm_env is a fixture)
from .conftest import make_user

SA = make_user("SUPER_ADMIN")
_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("review_f26", default="unset")


@pytest.mark.asyncio
async def test_run_blocking_runs_on_the_module_pool_like_to_thread():
    _VAR.set("copied")

    def work(a: int, *, b: int) -> tuple[str, str, int]:
        return threading.current_thread().name, _VAR.get(), a + b

    name, seen, total = await threads.run_blocking(work, 1, b=2)
    assert name.startswith(threads.THREAD_NAME_PREFIX) and seen == "copied" and total == 3

    def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await threads.run_blocking(fail)
    assert threads.executor() is threads.executor()


@pytest.mark.asyncio
async def test_sync_connection_test_and_health_probe_never_wait_for_the_default_executor(crm_env: CrmEnv):
    """Every thread of the default executor is busy (as when chat's pool is saturated): a
    connection test, a whole sync and the Microsoft health check still finish, on doc-intel's pool."""
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-executor")
    loop.set_default_executor(default)
    release = threading.Event()
    blocker = loop.run_in_executor(None, release.wait, 30)
    names: list[str] = []
    graph = crm_env.graph
    real_token = graph.acquire_token

    def token() -> None:
        names.append(threading.current_thread().name)
        real_token()

    graph.acquire_token = token  # type: ignore[method-assign]
    graph.on_download = lambda _file: names.append(threading.current_thread().name)
    graph.files = [remote("a"), remote("b")]
    try:
        source_id = (await crm_env.service.create_source(SA, SourceCreate(**source_body()))).id
        test = await asyncio.wait_for(crm_env.service.test_connection(source_id), 5)
        assert test.ok is True
        _, status = await asyncio.wait_for(crm_env.sync(source_id), 5)
        assert status == "COMPLETED"
        health.reset_throttle()
        overview = await asyncio.wait_for(run_checks(crm_env.health_deps(), component="microsoft_graph"), 5)
        assert next(c for c in overview.components if c.key == "microsoft_graph").status == "HEALTHY"
    finally:
        release.set()
        await blocker
        default.shutdown(wait=False)
    # sync: token + 2 downloads; health: token
    assert len(names) == 4 and all(n.startswith(threads.THREAD_NAME_PREFIX) for n in names), names


@pytest.mark.asyncio
async def test_a_busy_doc_intel_pool_leaves_the_default_executor_free():
    release = threading.Event()
    busy = [asyncio.ensure_future(threads.run_blocking(release.wait, 30)) for _ in range(threads.MAX_WORKERS + 2)]
    try:
        await asyncio.sleep(0.05)
        # chat's knowledge search path (asyncio.to_thread on the default executor) is not queued behind them
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "search results"), 2) == "search results"
    finally:
        release.set()
        await asyncio.gather(*busy)
