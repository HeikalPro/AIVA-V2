from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.auth.deps import ROLE_AGENT, ROLE_SUPERVISOR, UserContext, require_account_access, require_roles
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.exceptions import BadRequestError, ForbiddenError, NotFoundError
from backend.schemas.chat import MessageCreate, MessageOut, SessionCreate, SessionOut
from backend.services.rag import stream_rag_response
from backend.utils import serialize_row

router = APIRouter(prefix="/chat", tags=["chat"])


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
    return [MessageOut(**serialize_row(r) or {}) for r in rows]


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

    await db.execute(
        """
        INSERT INTO AIVA_chat_messages (session_id, sender_type, message_text)
        VALUES (:session_id, 'USER', :message_text)
        """,
        {"session_id": session_id, "message_text": body.message_text},
    )

    async def event_stream():
        final_result = None
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

        if final_result:
            await db.execute(
                """
                INSERT INTO AIVA_chat_messages (
                    session_id, sender_type, message_text,
                    prompt_tokens, completion_tokens, latency_ms
                ) VALUES (
                    :session_id, 'AI', :message_text,
                    :prompt_tokens, :completion_tokens, :latency_ms
                )
                """,
                {
                    "session_id": session_id,
                    "message_text": final_result.full_text,
                    "prompt_tokens": final_result.prompt_tokens,
                    "completion_tokens": final_result.completion_tokens,
                    "latency_ms": final_result.latency_ms,
                },
            )
            await db.execute(
                """
                INSERT INTO AIVA_ai_requests (
                    session_id, model_name, provider, input_tokens,
                    output_tokens, response_time_ms, total_cost, status
                ) VALUES (
                    :session_id, :model_name, :provider, :input_tokens,
                    :output_tokens, :response_time_ms, :total_cost, 'SUCCESS'
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
                },
            )
            yield f"data: {json.dumps({'type': 'done', 'latency_ms': final_result.latency_ms})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
