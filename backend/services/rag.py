from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from llm_service.client import LLMClient
from llm_service.config.provider_config import (
    OpenAIProviderConfig,
    OpenRouterProviderConfig,
    VLLMProviderConfig,
)
from llm_service.config.settings import LibrarySettings

from backend.config import Settings, get_settings
from backend.database import Database
from backend.services.llm_cost import estimate_llm_cost_usd
from backend.services.system_prompt import get_system_prompt_text
from embedding_service.service import EmbeddingService

_log = logging.getLogger(__name__)


@dataclass
class StreamResult:
    full_text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_cost: float | None = None
    latency_ms: int = 0
    model_name: str = ""
    provider: str = ""
    chunks_used: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def format_llm_error(exc: Exception) -> str:
    """Turn provider/HTTP failures into a short user-facing message."""
    raw = str(exc).strip()
    if not raw:
        return "The language model request failed."
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                message = err.get("message")
                if message:
                    return str(message).strip()
            message = data.get("message")
            if message:
                return str(message).strip()
    except json.JSONDecodeError:
        pass
    if len(raw) > 500:
        return raw[:500] + "…"
    return raw


def _format_context(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(No relevant knowledge base entries found.)"
    parts: list[str] = []
    for i, ch in enumerate(chunks, start=1):
        text = ch.get("text") or ch.get("content") or str(ch)
        parts.append(f"[{i}] {text}")
    return "\n\n".join(parts)


def _provider_config(provider: str, llm_row: dict[str, Any] | None) -> Any | None:
    if not llm_row:
        return None
    base_url = llm_row.get("api_base_url")
    if not base_url:
        return None
    url = str(base_url).rstrip("/")
    key = provider.lower()
    if key == "openai":
        return OpenAIProviderConfig(base_url=url)
    if key == "vllm":
        return VLLMProviderConfig(base_url=url)
    if key == "openrouter":
        return OpenRouterProviderConfig(base_url=url)
    return None


def _build_llm_client(llm_row: dict[str, Any] | None, settings: Settings) -> LLMClient:
    provider = (llm_row or {}).get("provider") or settings.llm_default_provider
    model = (llm_row or {}).get("model_name") or settings.llm_default_model
    lib_settings = LibrarySettings(
        default_provider=str(provider),
        default_model=str(model),
    )
    provider_config = _provider_config(str(provider), llm_row)
    return LLMClient(
        provider=str(provider),
        model=str(model),
        settings=lib_settings,
        config=provider_config,
    )


async def load_active_prompt(db: Database, account_id: int) -> tuple[str, str | None]:
    row = await db.fetch_one(
        """
        SELECT prompt_text, prompt_type
        FROM AIVA_prompts
        WHERE account_id = :account_id AND is_active = 1
        ORDER BY version_number DESC
        FETCH FIRST 1 ROW ONLY
        """,
        {"account_id": account_id},
    )
    if row:
        return str(row["prompt_text"]), row.get("prompt_type")
    return await get_system_prompt_text(db), None


async def load_llm_config(db: Database, account_id: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        SELECT lc.provider, lc.model_name, lc.temperature, lc.max_tokens,
               lc.api_base_url, lc.embedding_model, lc.reranker_model
        FROM AIVA_accounts a
        LEFT JOIN AIVA_llm_configs lc ON lc.id = a.llm_config_id
        WHERE a.id = :account_id
        """,
        {"account_id": account_id},
    )


async def load_conversation_history(
    db: Database,
    session_id: int,
    *,
    limit: int = 20,
) -> list[dict[str, str]]:
    rows = await db.fetch_all(
        """
        SELECT sender_type, message_text
        FROM AIVA_chat_messages
        WHERE session_id = :session_id
        ORDER BY created_at ASC
        FETCH FIRST :limit ROWS ONLY
        """,
        {"session_id": session_id, "limit": limit},
    )
    messages: list[dict[str, str]] = []
    for row in rows:
        role = "assistant" if str(row["sender_type"]).upper() in ("AI", "ASSISTANT") else "user"
        messages.append({"role": role, "content": str(row["message_text"])})
    return messages


async def search_knowledge(
    embedding_svc: EmbeddingService,
    corpus_id: str,
    query: str,
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        embedding_svc.search,
        corpus_id,
        query,
        top_k=top_k,
    )


async def stream_rag_response(
    db: Database,
    embedding_svc: EmbeddingService,
    *,
    account_id: int,
    corpus_id: str,
    session_id: int,
    user_message: str,
    top_k: int | None = None,
) -> AsyncIterator[tuple[str, StreamResult | None]]:
    settings = get_settings()
    tk = top_k or settings.search_default_top_k
    start = time.perf_counter()

    system_template, _ = await load_active_prompt(db, account_id)
    llm_row = await load_llm_config(db, account_id)
    history = await load_conversation_history(db, session_id)

    try:
        chunks = await search_knowledge(embedding_svc, corpus_id, user_message, top_k=tk)
    except Exception:
        _log.exception("KB search failed for account %s", account_id)
        chunks = []

    context = _format_context(chunks)
    system_prompt = system_template.replace("{context}", context)
    if "{context}" not in system_template:
        system_prompt = f"{system_template}\n\nKnowledge base context:\n{context}"

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    client = _build_llm_client(llm_row, settings)
    temperature = float(llm_row["temperature"]) if llm_row and llm_row.get("temperature") is not None else 0.7
    max_tokens = int(llm_row["max_tokens"]) if llm_row and llm_row.get("max_tokens") is not None else None

    result = StreamResult(
        model_name=str((llm_row or {}).get("model_name") or settings.llm_default_model),
        provider=str((llm_row or {}).get("provider") or settings.llm_default_provider),
        chunks_used=chunks,
    )

    try:
        async for chunk in client.astream(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            text = chunk.delta or ""
            if chunk.usage:
                if chunk.usage.prompt_tokens:
                    result.prompt_tokens = chunk.usage.prompt_tokens
                if chunk.usage.completion_tokens:
                    result.completion_tokens = chunk.usage.completion_tokens
            if text:
                result.full_text += text
                yield text, None
    except Exception as exc:
        _log.exception("LLM stream failed for account %s", account_id)
        result.error = format_llm_error(exc)
    finally:
        await client.provider.aclose()

    result.latency_ms = int((time.perf_counter() - start) * 1000)
    result.total_cost = estimate_llm_cost_usd(
        model_name=result.model_name,
        input_tokens=result.prompt_tokens,
        output_tokens=result.completion_tokens,
        settings=settings,
    )
    yield "", result
