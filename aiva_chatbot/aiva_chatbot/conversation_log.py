"""Best-effort conversation logging: POST each widget turn to the backend.

This module is deliberately fail-safe. Every path is wrapped so that a logging
problem (backend down, timeout, bad config) can never affect the chat response.
"""

from __future__ import annotations

import logging
from typing import Any

from aiva_chatbot.bot import ChatTurnResult

_log = logging.getLogger(__name__)

_CHUNK_TEXT_CHARS = 500
_TIMEOUT_SECONDS = 5.0


def _build_payload(
    *,
    corpus_id: str | None,
    query: str | None,
    top_k: int | None,
    vertical: str | None,
    result: ChatTurnResult,
) -> dict[str, Any]:
    chunks = []
    for h in result.retrieval or []:
        score = h.get("score")
        chunks.append(
            {
                "parent_id": (str(h.get("parent_id")) if h.get("parent_id") is not None else None),
                "chunk_index": h.get("chunk_index"),
                "score": float(score) if isinstance(score, (int, float)) else None,
                "text": str(h.get("text", ""))[:_CHUNK_TEXT_CHARS],
            }
        )

    usage = result.llm.usage
    return {
        "corpus_id": corpus_id,
        "query_text": query,
        "top_k": top_k,
        "verticals": [vertical] if vertical else None,
        "chunks": chunks,
        "model_name": result.llm.model,
        "provider": result.llm.provider,
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
        "response_time_ms": int(result.llm.latency_ms) if result.llm.latency_ms is not None else None,
        "total_cost": result.llm.cost_usd,
    }


async def log_widget_turn(
    *,
    backend_log_url: str,
    log_secret: str,
    corpus_id: str | None,
    query: str | None,
    top_k: int | None,
    vertical: str | None,
    result: ChatTurnResult,
) -> None:
    """Fire-and-forget POST of one turn. Never raises."""
    if not backend_log_url or not log_secret:
        return
    try:
        import httpx

        payload = _build_payload(
            corpus_id=corpus_id,
            query=query,
            top_k=top_k,
            vertical=vertical,
            result=result,
        )
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                backend_log_url,
                json=payload,
                headers={"X-Widget-Log-Secret": log_secret},
            )
        if resp.status_code >= 400:
            _log.warning("Widget turn logging rejected: HTTP %s", resp.status_code)
    except Exception:
        # Logging must never break chat — swallow everything.
        _log.warning("Failed to log widget turn", exc_info=True)


def _error_url(backend_log_url: str) -> str:
    """Derive the widget-error URL from the widget-turn URL (same base path)."""
    base = backend_log_url.rsplit("/", 1)[0]
    return f"{base}/widget-error"


async def log_widget_error(
    *,
    backend_log_url: str,
    log_secret: str,
    corpus_id: str | None,
    query: str | None,
    exception_type: str,
    exception_message: str | None,
    stack_trace: str | None,
    status_code: int | None,
) -> None:
    """Fire-and-forget POST of one failed turn (with traceback). Never raises."""
    if not backend_log_url or not log_secret:
        return
    try:
        import httpx

        payload = {
            "corpus_id": corpus_id,
            "query_text": query,
            "exception_type": exception_type,
            "exception_message": exception_message,
            "stack_trace": stack_trace,
            "status_code": status_code,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _error_url(backend_log_url),
                json=payload,
                headers={"X-Widget-Log-Secret": log_secret},
            )
        if resp.status_code >= 400:
            _log.warning("Widget error logging rejected: HTTP %s", resp.status_code)
    except Exception:
        # Logging must never break chat — swallow everything.
        _log.warning("Failed to log widget error", exc_info=True)
