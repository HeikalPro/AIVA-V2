from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

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

_LLM_SERVICE_ENV = Path(__file__).resolve().parents[2] / "llm_service" / ".env"
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
_OFFICIAL_OPENAI_BASE = "https://api.openai.com/v1"
_CHAT_KEY_ENV_NAMES = ("SOVEREIGNEG_API_KEY", "LLM_CHAT_API_KEY", "OPENAI_API_KEY")


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_dotenv(path: Path, name: str) -> str:
    if path.is_file():
        for line in _read_text_file(path).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() != name:
                continue
            return value.strip().strip('"').strip("'")
    return os.environ.get(name, "").strip()


def _read_llm_service_env(name: str) -> str:
    """Read one variable from llm_service/.env."""
    return _read_dotenv(_LLM_SERVICE_ENV, name)


def _read_root_env(name: str) -> str:
    """Read one variable from AIVA-V2/.env (official OpenAI key for embeddings + default chat)."""
    return _read_dotenv(_ROOT_ENV, name)


def _chat_api_key_for_custom_endpoint() -> SecretStr | None:
    for name in _CHAT_KEY_ENV_NAMES:
        raw = _read_llm_service_env(name)
        if raw:
            return SecretStr(raw)
    return None


def _official_openai_provider_config() -> OpenAIProviderConfig:
    """Official OpenAI — never use llm_service OPENAI_BASE_URL (e.g. SovereignEG)."""
    kwargs: dict[str, Any] = {"base_url": _OFFICIAL_OPENAI_BASE}
    raw = _read_root_env("OPENAI_API_KEY")
    if raw:
        kwargs["api_key"] = SecretStr(raw)
    return OpenAIProviderConfig(**kwargs)


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
    sources: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


def _normalize_parent_id(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    return str(value).strip().lstrip("/")


def _normalize_kb_url_template(template: str) -> str:
    text = (template or "").strip()
    return re.sub(r"=\s*/\s*$", "=", text)


def sanitize_kb_source_url(url: str) -> str:
    """Fix legacy links like …?id=/1495 → …?id=1495."""
    cleaned = (url or "").strip()
    if not cleaned:
        return cleaned
    return re.sub(r"(=)/+(?=[^/&\s])", r"\1", cleaned)


def build_kb_source_url(template: str, parent_id: str) -> str:
    """Build article URL from account/env template and external_parent_id.

    Use `{id}` anywhere in the template, or put the ID at the end (e.g. …/view.php?id=).
    """
    base = _normalize_kb_url_template(template)
    article_id = _normalize_parent_id(parent_id)
    if not base or not article_id:
        return ""
    if "{id}" in base:
        url = base.replace("{id}", article_id)
    else:
        url = f"{base}{article_id}"
    return sanitize_kb_source_url(url)


def build_kb_sources(
    chunks: list[dict[str, Any]],
    base_url: str,
    *,
    max_count: int = 1,
) -> list[dict[str, str]]:
    """Build KB article links from retrieved chunks (one per unique external_parent_id, best-first)."""
    base = _normalize_kb_url_template(base_url or "")
    if not base or max_count < 1:
        return []
    seen: set[str] = set()
    sources: list[dict[str, str]] = []
    for ch in chunks:
        parent_id = _normalize_parent_id(ch.get("parent_id"))
        if not parent_id:
            payload = ch.get("payload") or {}
            if isinstance(payload, dict):
                parent_id = _normalize_parent_id(payload.get("canonical_id") or payload.get("id"))
        if not parent_id or parent_id in seen:
            continue
        seen.add(parent_id)
        sources.append({"parent_id": parent_id, "url": build_kb_source_url(base, parent_id)})
        if len(sources) >= max_count:
            break
    return sources


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
    key = provider.lower()
    base_url = (llm_row or {}).get("api_base_url")
    if key == "openai":
        if not base_url:
            return _official_openai_provider_config()
        url = str(base_url).rstrip("/")
        chat_key = _chat_api_key_for_custom_endpoint()
        kwargs: dict[str, Any] = {"base_url": url}
        if chat_key is not None:
            kwargs["api_key"] = chat_key
        return OpenAIProviderConfig(**kwargs)
    if not base_url:
        return None
    url = str(base_url).rstrip("/")
    chat_key = _chat_api_key_for_custom_endpoint()
    if key == "vllm":
        kwargs = {"base_url": url}
        if chat_key is not None:
            kwargs["api_key"] = chat_key
        return VLLMProviderConfig(**kwargs)
    if key == "openrouter":
        kwargs = {"base_url": url}
        if chat_key is not None:
            kwargs["api_key"] = chat_key
        return OpenRouterProviderConfig(**kwargs)
    return None


def _build_llm_client(llm_row: dict[str, Any] | None, settings: Settings) -> LLMClient:
    provider = (llm_row or {}).get("provider") or settings.llm_default_provider
    model = (llm_row or {}).get("model_name") or settings.llm_default_model
    lib_settings = LibrarySettings(
        default_provider=str(provider),
        default_model=str(model),
        _env_file=None,
    )
    provider_config = _provider_config(str(provider), llm_row)
    if provider_config is None and str(provider).lower() == "openai":
        provider_config = _official_openai_provider_config()
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


def resolve_kb_source_base_url(account_kb_url: str | None, settings: Settings) -> str:
    """Per-account KB link template, falling back to global KB_SOURCE_BASE_URL."""
    custom = (account_kb_url or "").strip()
    if custom:
        return _normalize_kb_url_template(custom)
    return _normalize_kb_url_template(settings.kb_source_base_url or "")


async def stream_rag_response(
    db: Database,
    embedding_svc: EmbeddingService,
    *,
    account_id: int,
    corpus_id: str,
    session_id: int,
    user_message: str,
    top_k: int | None = None,
    kb_source_base_url: str | None = None,
) -> AsyncIterator[tuple[str, StreamResult | None]]:
    settings = get_settings()
    tk = top_k or settings.search_default_top_k
    start = time.perf_counter()
    kb_base = resolve_kb_source_base_url(kb_source_base_url, settings)

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
        sources=build_kb_sources(
            chunks,
            kb_base,
            max_count=settings.kb_source_max_count,
        ),
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
