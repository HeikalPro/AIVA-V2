from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.auth.deps import ROLE_AGENT, ROLE_SUPER_ADMIN, ROLE_SUPERVISOR, UserContext, require_roles, require_roles_or_nav_permission
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import BadRequestError, ForbiddenError, NotFoundError
from backend.schemas.chat import (
    KbSourceOut,
    MessageCreate,
    MessageOut,
    MessageRatingCreate,
    MessageRatingOut,
    SessionCreate,
    SessionOut,
    SessionQueuesUpdate,
)
from backend.schemas.kb_queues import ChatQueueAccessOut, QueueGroupOut
from backend.services.agent_queue_access import get_allowed_queue_keys
from backend.services.chat_queues import (
    default_active_queues_for_user,
    load_account_corpus_config,
    normalize_session_active_queues,
    resolve_search_verticals_for_session,
    session_out_from_row,
)
from backend.services.kb_queue_groups import dumps_active_queues_json, list_queue_catalog
from backend.services.widget_features import parse_widget_features
from backend.config import get_settings
from backend.services.rag import format_llm_error, load_llm_config, sanitize_kb_source_url, stream_rag_response
from backend.services.rag_retrieval_log import persist_rag_retrieval
from backend.services.sse_metrics import count_stream
from backend.utils import serialize_row

router = APIRouter(prefix="/chat", tags=["chat"])


def _assistant_text_for_db(full_text: str, error: str | None) -> str:
    """Oracle treats empty VARCHAR2 as NULL; MESSAGE_TEXT is NOT NULL."""
    text = (full_text or "").strip()
    if text:
        if error:
            return f"{text}\n\nError: {error}"
        return text
    if error:
        return f"Error: {error}"
    return "(Empty reply from stream.)"


def _parse_kb_sources(raw: object | None) -> list[KbSourceOut]:
    if raw is None:
        return []
    text = raw.read() if hasattr(raw, "read") else raw
    if not text:
        return []
    try:
        parsed = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[KbSourceOut] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        parent_id = item.get("parent_id")
        url = item.get("url")
        if parent_id is None or not url:
            continue
        out.append(
            KbSourceOut(
                parent_id=str(parent_id).strip().lstrip("/"),
                url=sanitize_kb_source_url(str(url)),
            )
        )
    return out


def _message_out(row: dict) -> MessageOut:
    data = serialize_row(row) or {}
    rating_raw = data.get("agent_rating")
    rating = str(rating_raw).lower() if rating_raw is not None else None
    return MessageOut(
        id=int(data["id"]),
        session_id=int(data["session_id"]),
        sender_type=str(data["sender_type"]),
        message_text=str(data["message_text"]),
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
        latency_ms=data.get("latency_ms"),
        created_at=data.get("created_at"),
        rating=rating if rating in ("up", "down") else None,
        feedback=data.get("agent_feedback"),
        rated_at=data.get("rated_at"),
        sources=_parse_kb_sources(data.get("kb_sources_json")),
    )


@router.get("/queue-access", response_model=ChatQueueAccessOut)
async def get_chat_queue_access(
    account_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
) -> ChatQueueAccessOut:
    account = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    if not user.can_access_account(account_id, int(account["organization_id"])):
        raise ForbiddenError("No access to this account")

    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, account_id)
    catalog = list_queue_catalog(corpus_config)
    allowed = await get_allowed_queue_keys(
        db, user, account_id=account_id, corpus_config=corpus_config
    )

    # Per-account admin override: hide any KB button not in the visibility allow-list.
    features = parse_widget_features(account.get("widget_features"))
    visible_keys = features.kb_visible_keys()
    if visible_keys is not None:
        visible_set = set(visible_keys)
        allowed = [key for key in allowed if key in visible_set]

    return ChatQueueAccessOut(
        account_id=account_id,
        available_queues=[QueueGroupOut(**item) for item in catalog if item["key"] in allowed],
        allowed_queues=allowed,
        default_active_queues=default_active_queues_for_user(allowed),
    )


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
) -> SessionOut:
    account = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
    if not account:
        raise NotFoundError("Account not found")
    if not user.can_access_account(body.account_id, int(account["organization_id"])):
        raise ForbiddenError("No access to this account")

    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, body.account_id)
    active_queues = await normalize_session_active_queues(
        db,
        user,
        account_id=body.account_id,
        corpus_config=corpus_config,
        requested=body.active_queues,
    )
    active_queues_json = dumps_active_queues_json(active_queues)

    session_id = await db.execute(
        """
        INSERT INTO AIVA_chat_sessions (account_id, user_id, session_status, active_queues)
        VALUES (:account_id, :user_id, 'ACTIVE', :active_queues)
        RETURNING id INTO :out_id
        """,
        {
            "account_id": body.account_id,
            "user_id": user.id,
            "active_queues": active_queues_json,
        },
        return_id=True,
    )
    row = await db.fetch_one(
        """
        SELECT cs.*,
               u.first_name AS agent_first_name,
               u.last_name AS agent_last_name,
               u.email AS agent_email
        FROM AIVA_chat_sessions cs
        JOIN AIVA_users u ON u.id = cs.user_id
        WHERE cs.id = :id
        """,
        {"id": session_id},
    )
    return session_out_from_row(row or {})


@router.patch("/sessions/{session_id}/queues", response_model=SessionOut)
async def update_session_queues(
    session_id: int,
    body: SessionQueuesUpdate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
) -> SessionOut:
    session = await db.fetch_one("SELECT * FROM AIVA_chat_sessions WHERE id = :id", {"id": session_id})
    if not session:
        raise NotFoundError("Session not found")
    if int(session["user_id"]) != user.id:
        raise ForbiddenError("Cannot update another agent's session")

    account_id = int(session["account_id"])
    _corpus_id, corpus_config = await load_account_corpus_config(db, embedding_svc, account_id)
    active_queues = await normalize_session_active_queues(
        db,
        user,
        account_id=account_id,
        corpus_config=corpus_config,
        requested=body.active_queues,
    )
    if active_queues is None:
        raise BadRequestError("At least one queue must be selected")

    await db.execute(
        """
        UPDATE AIVA_chat_sessions
        SET active_queues = :active_queues
        WHERE id = :id
        """,
        {"id": session_id, "active_queues": dumps_active_queues_json(active_queues)},
    )
    row = await db.fetch_one(
        """
        SELECT cs.*,
               u.first_name AS agent_first_name,
               u.last_name AS agent_last_name,
               u.email AS agent_email,
               (SELECT COUNT(*) FROM AIVA_chat_messages cm WHERE cm.session_id = cs.id) AS message_count
        FROM AIVA_chat_sessions cs
        JOIN AIVA_users u ON u.id = cs.user_id
        WHERE cs.id = :id
        """,
        {"id": session_id},
    )
    return session_out_from_row(row or {})


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    account_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
) -> list[SessionOut]:
    account = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    if not user.can_access_account(account_id, int(account["organization_id"])):
        raise ForbiddenError("No access to this account")

    params: dict = {"account_id": account_id}
    user_clause = ""
    if user.has_role(ROLE_AGENT) and not user.has_role(ROLE_SUPERVISOR):
        user_clause = "AND cs.user_id = :user_id"
        params["user_id"] = user.id

    rows = await db.fetch_all(
        f"""
        SELECT cs.*,
               u.first_name AS agent_first_name,
               u.last_name AS agent_last_name,
               u.email AS agent_email,
               (SELECT COUNT(*) FROM AIVA_chat_messages cm WHERE cm.session_id = cs.id) AS message_count
        FROM AIVA_chat_sessions cs
        JOIN AIVA_users u ON u.id = cs.user_id
        WHERE cs.account_id = :account_id {user_clause}
        ORDER BY cs.id DESC
        FETCH FIRST 50 ROWS ONLY
        """,
        params,
    )
    return [session_out_from_row(r) for r in rows]


@router.get("/sessions/{session_id}", response_model=list[MessageOut])
async def get_session_messages(
    session_id: int,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
) -> list[MessageOut]:
    session = await db.fetch_one("SELECT * FROM AIVA_chat_sessions WHERE id = :id", {"id": session_id})
    if not session:
        raise NotFoundError("Session not found")

    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": int(session["account_id"])},
    )
    if user.has_role(ROLE_AGENT) and int(session["user_id"]) != user.id:
        raise ForbiddenError("Cannot view another agent's session")
    if account and not user.can_access_account(int(session["account_id"]), int(account["organization_id"])):
        raise ForbiddenError("No access to this session")

    rows = await db.fetch_all(
        """
        SELECT * FROM AIVA_chat_messages
        WHERE session_id = :session_id
        ORDER BY created_at ASC
        """,
        {"session_id": session_id},
    )
    return [_message_out(r) for r in rows]


@router.get("/ratings", response_model=list[MessageRatingOut])
async def list_message_ratings(
    user: Annotated[UserContext, Depends(require_roles(ROLE_SUPER_ADMIN))],
    db: DbDep,
) -> list[MessageRatingOut]:
    if not user.is_super_admin:
        raise ForbiddenError("Super Admin only")

    rows = await db.fetch_all(
        """
        SELECT cm.id AS message_id,
               cm.session_id,
               cm.message_text,
               cm.agent_rating AS rating,
               cm.agent_feedback AS feedback,
               cm.rated_at,
               cs.account_id,
               cs.user_id AS agent_user_id,
               a.name AS account_name,
               a.organization_id,
               o.name AS organization_name,
               u.email AS agent_email,
               u.first_name AS agent_first_name,
               u.last_name AS agent_last_name
        FROM AIVA_chat_messages cm
        JOIN AIVA_chat_sessions cs ON cs.id = cm.session_id
        JOIN AIVA_accounts a ON a.id = cs.account_id
        JOIN AIVA_organizations o ON o.id = a.organization_id
        JOIN AIVA_users u ON u.id = cs.user_id
        WHERE cm.agent_rating IS NOT NULL
          AND UPPER(cm.sender_type) IN ('AI', 'ASSISTANT')
        ORDER BY cm.rated_at DESC NULLS LAST, cm.id DESC
        FETCH FIRST 500 ROWS ONLY
        """
    )
    out: list[MessageRatingOut] = []
    for row in rows:
        data = serialize_row(row) or {}
        rating = str(data.get("rating", "")).lower()
        if rating not in ("up", "down"):
            continue
        out.append(
            MessageRatingOut(
                message_id=int(data["message_id"]),
                session_id=int(data["session_id"]),
                account_id=int(data["account_id"]),
                account_name=data.get("account_name"),
                organization_id=int(data["organization_id"]),
                organization_name=data.get("organization_name"),
                agent_user_id=int(data["agent_user_id"]),
                agent_email=data.get("agent_email"),
                agent_first_name=data.get("agent_first_name"),
                agent_last_name=data.get("agent_last_name"),
                message_text=str(data.get("message_text") or ""),
                rating=rating,
                feedback=data.get("feedback"),
                rated_at=data.get("rated_at"),
            )
        )
    return out


@router.post("/messages/{message_id}/rating", response_model=MessageOut)
async def rate_message(
    message_id: int,
    body: MessageRatingCreate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
) -> MessageOut:
    message = await db.fetch_one(
        "SELECT * FROM AIVA_chat_messages WHERE id = :id",
        {"id": message_id},
    )
    if not message:
        raise NotFoundError("Message not found")

    sender = str(message.get("sender_type", "")).upper()
    if sender not in ("AI", "ASSISTANT"):
        raise BadRequestError("Only assistant messages can be rated")

    session = await db.fetch_one(
        "SELECT * FROM AIVA_chat_sessions WHERE id = :id",
        {"id": int(message["session_id"])},
    )
    if not session:
        raise NotFoundError("Session not found")

    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": int(session["account_id"])},
    )
    if user.has_role(ROLE_AGENT) and int(session["user_id"]) != user.id:
        raise ForbiddenError("Cannot rate messages in another agent's session")
    if account and not user.can_access_account(int(session["account_id"]), int(account["organization_id"])):
        raise ForbiddenError("No access to this session")

    feedback = (body.feedback or "").strip() if body.rating == "down" else None

    await db.execute(
        """
        UPDATE AIVA_chat_messages
        SET agent_rating = :rating,
            agent_feedback = :feedback,
            rated_at = SYSTIMESTAMP,
            rated_by_user_id = :rated_by_user_id
        WHERE id = :message_id
        """,
        {
            "message_id": message_id,
            "rating": body.rating,
            "feedback": feedback,
            "rated_by_user_id": user.id,
        },
    )

    updated = await db.fetch_one(
        "SELECT * FROM AIVA_chat_messages WHERE id = :id",
        {"id": message_id},
    )
    return _message_out(updated or {})


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: int,
    body: MessageCreate,
    user: Annotated[UserContext, Depends(require_roles_or_nav_permission("chat", ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
) -> StreamingResponse:
    session = await db.fetch_one("SELECT * FROM AIVA_chat_sessions WHERE id = :id", {"id": session_id})
    if not session:
        raise NotFoundError("Session not found")
    if int(session["user_id"]) != user.id:
        raise ForbiddenError("Cannot send messages to another agent's session")

    account = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": int(session["account_id"])})
    if not account:
        raise NotFoundError("Account not found")
    corpus_id = account.get("corpus_id")
    if not corpus_id:
        raise BadRequestError("Account has no knowledge base (corpus_id) configured")

    _cid, corpus_config = await load_account_corpus_config(db, embedding_svc, int(session["account_id"]))
    search_verticals = await resolve_search_verticals_for_session(
        db,
        user,
        account_id=int(session["account_id"]),
        corpus_config=corpus_config,
        session_row=session,
    )

    user_message_id = await db.execute(
        """
        INSERT INTO AIVA_chat_messages (session_id, sender_type, message_text)
        VALUES (:session_id, 'USER', :message_text)
        RETURNING id INTO :out_id
        """,
        {"session_id": session_id, "message_text": body.message_text},
        return_id=True,
    )

    # Labels used to attribute a request to its provider/model even when it fails
    # before the stream produces a result (e.g. "model does not exist").
    _settings = get_settings()
    _llm_row = await load_llm_config(db, int(session["account_id"]))
    model_label = str((_llm_row or {}).get("model_name") or _settings.llm_default_model)
    provider_label = str((_llm_row or {}).get("provider") or _settings.llm_default_provider)

    async def event_stream():
        final_result = None
        assistant_message_id: int | None = None
        try:
            async for chunk, result in stream_rag_response(
                db,
                embedding_svc,
                account_id=int(session["account_id"]),
                corpus_id=str(corpus_id),
                session_id=session_id,
                user_message=body.message_text,
                top_k=body.top_k,
                kb_source_base_url=account.get("kb_source_base_url"),
                verticals=search_verticals,
            ):
                if chunk:
                    yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n"
                if result is not None:
                    final_result = result
        except Exception as exc:
            error_text = format_llm_error(exc)
            try:
                await db.execute(
                    """
                    INSERT INTO AIVA_ai_requests (
                        session_id, account_id, model_name, provider, input_tokens,
                        output_tokens, response_time_ms, total_cost, status, error_message
                    ) VALUES (
                        :session_id, :account_id, :model_name, :provider, 0,
                        0, NULL, NULL, 'FAILED', :error_message
                    )
                    """,
                    {
                        "session_id": session_id,
                        "account_id": int(session["account_id"]),
                        "model_name": model_label,
                        "provider": provider_label,
                        "error_message": error_text[:2000] or None,
                    },
                )
            except Exception:
                pass
            yield f"data: {json.dumps({'type': 'error', 'message': error_text})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'user_message_id': user_message_id})}\n\n"
            return

        if final_result:
            if final_result.error:
                yield f"data: {json.dumps({'type': 'error', 'message': final_result.error})}\n\n"
            elif not (final_result.full_text or "").strip():
                empty_msg = "The model returned an empty response."
                final_result.error = empty_msg
                yield f"data: {json.dumps({'type': 'error', 'message': empty_msg})}\n\n"
            assistant_text = _assistant_text_for_db(final_result.full_text, final_result.error)
            request_status = "FAILED" if final_result.error else "SUCCESS"
            if final_result.sources:
                final_result.sources = [
                    {
                        "parent_id": str(s.get("parent_id", "")).strip().lstrip("/"),
                        "url": sanitize_kb_source_url(str(s.get("url", ""))),
                    }
                    for s in final_result.sources
                    if s.get("parent_id") and s.get("url")
                ]
            kb_sources_json = json.dumps(final_result.sources) if final_result.sources else None
            assistant_message_id = await db.execute(
                """
                INSERT INTO AIVA_chat_messages (
                    session_id, sender_type, message_text,
                    prompt_tokens, completion_tokens, latency_ms, kb_sources_json
                ) VALUES (
                    :session_id, 'AI', :message_text,
                    :prompt_tokens, :completion_tokens, :latency_ms, :kb_sources_json
                )
                RETURNING id INTO :out_id
                """,
                {
                    "session_id": session_id,
                    "message_text": assistant_text,
                    "prompt_tokens": final_result.prompt_tokens,
                    "completion_tokens": final_result.completion_tokens,
                    "latency_ms": final_result.latency_ms,
                    "kb_sources_json": kb_sources_json,
                },
                return_id=True,
            )
            await db.execute(
                """
                INSERT INTO AIVA_ai_requests (
                    session_id, account_id, model_name, provider, input_tokens,
                    output_tokens, response_time_ms, total_cost, status, error_message
                ) VALUES (
                    :session_id, :account_id, :model_name, :provider, :input_tokens,
                    :output_tokens, :response_time_ms, :total_cost, :status, :error_message
                )
                """,
                {
                    "session_id": session_id,
                    "account_id": int(session["account_id"]),
                    "model_name": final_result.model_name,
                    "provider": final_result.provider,
                    "input_tokens": final_result.prompt_tokens or 0,
                    "output_tokens": final_result.completion_tokens or 0,
                    "response_time_ms": final_result.latency_ms,
                    "total_cost": final_result.total_cost,
                    "status": request_status,
                    "error_message": (final_result.error or "")[:2000] or None,
                },
            )
            await persist_rag_retrieval(
                db,
                session_id=session_id,
                account_id=int(session["account_id"]),
                corpus_id=str(corpus_id),
                query_text=body.message_text,
                top_k=final_result.top_k_used or None,
                verticals=final_result.verticals_used,
                status=final_result.retrieval_status or "UNKNOWN",
                chunks=final_result.chunks_used,
                retrieval_ms=final_result.retrieval_ms or None,
                error_message=final_result.retrieval_error,
            )
            yield f"data: {json.dumps({'type': 'done', 'latency_ms': final_result.latency_ms, 'user_message_id': user_message_id, 'assistant_message_id': assistant_message_id, 'sources': final_result.sources, 'usage': {'input_tokens': final_result.prompt_tokens or 0, 'output_tokens': final_result.completion_tokens or 0, 'total_cost': final_result.total_cost}})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'done', 'user_message_id': user_message_id})}\n\n"

    return StreamingResponse(count_stream(event_stream()), media_type="text/event-stream")
