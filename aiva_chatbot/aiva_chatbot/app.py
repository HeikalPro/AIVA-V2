"""FastAPI application: ``POST /chat`` for frontend integration."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import oracledb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from llm_service.core.exceptions import (
    AuthenticationError,
    LLMServiceError,
    RateLimitError,
)

from aiva_chatbot.bot import Chatbot, ChatTurnResult, create_chatbot_from_env
from aiva_chatbot.env_bootstrap import preload_monorepo_dotenv
from aiva_chatbot.settings import ApiSettings

_log = logging.getLogger(__name__)


def _parse_cors(origins: str) -> list[str]:
    s = origins.strip()
    if s == "*":
        return ["*"]
    return [o.strip() for o in s.split(",") if o.strip()]


def _cors_kwargs(origins: str) -> dict:
    parsed = _parse_cors(origins)
    if parsed == ["*"]:
        return {"allow_origins": parsed, "allow_credentials": False}
    return {"allow_origins": parsed, "allow_credentials": True}


@asynccontextmanager
async def lifespan(app: FastAPI):
    preload_monorepo_dotenv()
    settings = ApiSettings()
    app.state.settings = settings
    app.state.chatbot = create_chatbot_from_env(
        settings.corpus_id,
        use_auth=settings.use_zoho_auth,
    )
    yield
    app.state.chatbot.close()


app = FastAPI(title="AIVA Chatbot API", version="0.1.0", lifespan=lifespan)
_cors_origin_env = os.environ.get("AIVA_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    **_cors_kwargs(_cors_origin_env),
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_chatbot(request: Request) -> Chatbot:
    return request.app.state.chatbot


ChatbotDep = Annotated[Chatbot, Depends(get_chatbot)]


class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Open-style chat: send prior turns plus the latest user message last."""

    messages: list[ChatMessageIn] = Field(min_length=1)
    corpus_id: str | None = Field(default=None, description="Override default corpus for this request only.")
    skip_retrieval: bool = False
    top_k: int | None = None
    vertical: str | None = None
    interaction_type: str | None = None
    issue_type: str | None = None
    escalation: str | None = None


class ChatMessageOut(BaseModel):
    role: Literal["assistant"]
    content: str


class RetrievalChunkOut(BaseModel):
    chunk_id: str
    text: str
    score: float | None = None


class ChatResponse(BaseModel):
    message: ChatMessageOut
    retrieval: list[RetrievalChunkOut]
    correlation_id: str
    model: str


def _to_response(result: ChatTurnResult) -> ChatResponse:
    chunks = [
        RetrievalChunkOut(
            chunk_id=str(h.get("chunk_id", "")),
            text=str(h.get("text", "")),
            score=float(h["score"]) if isinstance(h.get("score"), int | float) else None,
        )
        for h in result.retrieval
    ]
    return ChatResponse(
        message=ChatMessageOut(role="assistant", content=result.text),
        retrieval=chunks,
        correlation_id=result.llm.correlation_id,
        model=result.llm.model,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, bot: ChatbotDep) -> ChatResponse:
    raw = [m.model_dump() for m in body.messages]
    try:
        result = await bot.achat_with_messages(
            raw,
            corpus_id=body.corpus_id,
            top_k=body.top_k,
            vertical=body.vertical,
            interaction_type=body.interaction_type,
            issue_type=body.issue_type,
            escalation=body.escalation,
            skip_retrieval=body.skip_retrieval,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthenticationError:
        _log.warning("LLM authentication failed")
        raise HTTPException(
            status_code=401,
            detail="LLM provider rejected credentials or API key is missing. "
            "Set keys in llm_service/.env or repo .env (see OPENAI_API_KEY / LLM_*).",
        ) from None
    except RateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail="LLM provider rate limit exceeded.",
            headers={"Retry-After": str(int(exc.retry_after or 60))},
        ) from None
    except oracledb.exceptions.DatabaseError:
        _log.exception("Oracle error during chat")
        raise HTTPException(
            status_code=503,
            detail="Knowledge base is unavailable (database error). Check Oracle DSN and credentials.",
        ) from None
    except LLMServiceError as exc:
        _log.exception("LLM error during chat")
        raise HTTPException(status_code=502, detail=f"LLM request failed: {exc!s}") from None
    return _to_response(result)
