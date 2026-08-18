from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def persist_ai_request(
    db: Database | None,
    *,
    session_id: int | None,
    account_id: int | None,
    model_name: str | None,
    provider: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    response_time_ms: int | None,
    total_cost: float | None,
    status: str,
    error_message: str | None,
    source: str | None = None,
) -> None:
    """Best-effort write of one LLM call record; never raises to the caller.

    Used by non-chat surfaces (e.g. the widget) that don't own the inline
    INSERT in the agent chat path.
    """
    if db is None:
        return
    try:
        await db.execute(
            """
            INSERT INTO AIVA_ai_requests (
                session_id, account_id, model_name, provider, input_tokens,
                output_tokens, response_time_ms, total_cost, status, error_message, source
            ) VALUES (
                :session_id, :account_id, :model_name, :provider, :input_tokens,
                :output_tokens, :response_time_ms, :total_cost, :status, :error_message, :source
            )
            """,
            {
                "session_id": session_id,
                "account_id": account_id,
                "model_name": (model_name or "")[:255] or None,
                "provider": (provider or "")[:64] or None,
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "response_time_ms": response_time_ms,
                "total_cost": total_cost,
                "status": (status or "UNKNOWN")[:16],
                "error_message": (error_message or "")[:2000] or None,
                "source": (source or "")[:16] or None,
            },
        )
    except Exception:
        _log.warning("Failed to persist AI request log", exc_info=True)
