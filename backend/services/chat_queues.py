"""Chat session queue resolution helpers."""
from __future__ import annotations

from typing import Any

from backend.auth.deps import UserContext
from backend.database import Database
from backend.dependencies import EmbeddingServiceDep
from backend.exceptions import BadRequestError, NotFoundError
from backend.schemas.chat import SessionOut
from backend.services.agent_queue_access import get_allowed_queue_keys
from backend.services.kb_queue_groups import (
    dumps_active_queues_json,
    get_queue_groups_from_config,
    parse_active_queues_json,
    resolve_verticals,
    validate_active_queues,
)
from backend.utils import serialize_row


async def load_account_corpus_config(
    db: Database,
    embedding_svc: EmbeddingServiceDep,
    account_id: int,
) -> tuple[str, dict | None]:
    account = await db.fetch_one("SELECT corpus_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    corpus_id = account.get("corpus_id")
    if not corpus_id:
        raise BadRequestError("Account has no knowledge base (corpus_id) configured")
    corpus = embedding_svc.get_corpus(str(corpus_id))
    if not corpus:
        raise BadRequestError("Knowledge base corpus not found")
    config = corpus.get("config")
    return str(corpus_id), config if isinstance(config, dict) else None


async def normalize_session_active_queues(
    db: Database,
    user: UserContext,
    *,
    account_id: int,
    corpus_config: dict | None,
    requested: list[str] | None,
) -> list[str] | None:
    allowed = await get_allowed_queue_keys(
        db, user, account_id=account_id, corpus_config=corpus_config
    )
    if requested is None:
        return None
    return validate_active_queues(
        corpus_config,
        requested,
        allowed_queue_keys=allowed,
    )


def reconcile_active_queues_with_allowed(
    corpus_config: dict | None,
    active: list[str] | None,
    allowed: list[str],
) -> list[str] | None:
    """Drop queues the agent no longer has; fall back to all allowed if none remain."""
    if active is None:
        return None
    allowed_set = set(allowed)
    filtered = [key for key in active if key in allowed_set]
    if not filtered:
        filtered = list(allowed)
    return validate_active_queues(
        corpus_config,
        filtered,
        allowed_queue_keys=allowed,
    )


async def resolve_search_verticals_for_session(
    db: Database,
    user: UserContext,
    *,
    account_id: int,
    corpus_config: dict | None,
    session_row: dict[str, Any],
) -> list[str] | None:
    active = parse_active_queues_json(session_row.get("active_queues"))
    if active is None:
        return None
    allowed = await get_allowed_queue_keys(
        db, user, account_id=account_id, corpus_config=corpus_config
    )
    validated = reconcile_active_queues_with_allowed(
        corpus_config,
        active,
        allowed,
    )
    return resolve_verticals(corpus_config, validated)


def session_out_from_row(row: dict[str, Any]) -> SessionOut:
    data = serialize_row(row) or {}
    active = parse_active_queues_json(data.get("active_queues")) or []
    data["active_queues"] = active
    return SessionOut(**data)


def default_active_queues_for_user(allowed: list[str]) -> list[str]:
    return list(allowed)
