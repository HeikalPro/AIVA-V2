from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.auth.deps import ROLE_AGENT, ROLE_SUPER_ADMIN, ROLE_SUPERVISOR, UserContext, require_roles
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import BadRequestError, ForbiddenError, NotFoundError
from backend.schemas.chat import (
    MessageCreate,
    MessageOut,
    MessageRatingCreate,
    MessageRatingOut,
    SessionCreate,
    SessionOut,
)
from backend.services.rag import format_llm_error, stream_rag_response
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
    )


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate,
    user: Annotated[UserContext, Depends(require_roles(ROLE_AGENT, ROLE_SUPERVISOR))],
    db: DbDep,
) -> SessionOut:
    account = await db.fetch_one("SELECT * FROM AIVA_accounts WHERE id = :id", {"id": body.account_id})
    if not account:
        raise NotFoundError("Account not found")
    if not user.can_access_account(body.account_id, int(account["organization_id"])):
        raise ForbiddenError("No access to this account")

    session_id = await db.execute(
        """
        INSERT INTO AIVA_chat_sessions (account_id, user_id, session_status)
        VALUES (:account_id, :user_id, 'ACTIVE')
        RETURNING id INTO :out_id
        """,
        {"account_id": body.account_id, "user_id": user.id},
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
    return SessionOut(**serialize_row(row) or {})


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    account_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_AGENT, ROLE_SUPERVISOR))],
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
    return [SessionOut(**serialize_row(r) or {}) for r in rows]


@router.get("/sessions/{session_id}", response_model=list[MessageOut])
async def get_session_messages(
    session_id: int,
    user: Annotated[UserContext, Depends(require_roles(ROLE_AGENT, ROLE_SUPERVISOR))],
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
    user: Annotated[UserContext, Depends(require_roles(ROLE_AGENT, ROLE_SUPERVISOR))],
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
    user: Annotated[UserContext, Depends(require_roles(ROLE_AGENT))],
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

    user_message_id = await db.execute(
        """
        INSERT INTO AIVA_chat_messages (session_id, sender_type, message_text)
        VALUES (:session_id, 'USER', :message_text)
        RETURNING id INTO :out_id
        """,
        {"session_id": session_id, "message_text": body.message_text},
        return_id=True,
    )

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
            ):
                if chunk:
                    yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n"
                if result is not None:
                    final_result = result
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': format_llm_error(exc)})}\n\n"
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
            assistant_message_id = await db.execute(
                """
                INSERT INTO AIVA_chat_messages (
                    session_id, sender_type, message_text,
                    prompt_tokens, completion_tokens, latency_ms
                ) VALUES (
                    :session_id, 'AI', :message_text,
                    :prompt_tokens, :completion_tokens, :latency_ms
                )
                RETURNING id INTO :out_id
                """,
                {
                    "session_id": session_id,
                    "message_text": assistant_text,
                    "prompt_tokens": final_result.prompt_tokens,
                    "completion_tokens": final_result.completion_tokens,
                    "latency_ms": final_result.latency_ms,
                },
                return_id=True,
            )
            await db.execute(
                """
                INSERT INTO AIVA_ai_requests (
                    session_id, model_name, provider, input_tokens,
                    output_tokens, response_time_ms, total_cost, status
                ) VALUES (
                    :session_id, :model_name, :provider, :input_tokens,
                    :output_tokens, :response_time_ms, :total_cost, :status
                )
                """,
                {
                    "session_id": session_id,
                    "model_name": final_result.model_name,
                    "provider": final_result.provider,
                    "input_tokens": final_result.prompt_tokens or 0,
                    "output_tokens": final_result.completion_tokens or 0,
                    "response_time_ms": final_result.latency_ms,
                    "total_cost": final_result.total_cost,
                    "status": request_status,
                },
            )
            yield f"data: {json.dumps({'type': 'done', 'latency_ms': final_result.latency_ms, 'user_message_id': user_message_id, 'assistant_message_id': assistant_message_id})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'done', 'user_message_id': user_message_id})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
