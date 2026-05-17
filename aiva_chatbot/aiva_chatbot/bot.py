from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MONOREPO_PREPENDED = False


def _ensure_monorepo_imports() -> None:
    """Allow ``embedding_service`` and ``zoho_auth`` imports when this repo is not installed as packages."""
    global _MONOREPO_PREPENDED
    if _MONOREPO_PREPENDED:
        return
    try:
        import embedding_service  # noqa: F401
        import zoho_auth  # noqa: F401
    except ImportError:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        try:
            import embedding_service  # noqa: F401
            import zoho_auth  # noqa: F401
        except ImportError as exc:
            msg = (
                "Could not import ``embedding_service`` or ``zoho_auth``. "
                "Install or clone the AIVA-V2 monorepo and run with the repo root on ``PYTHONPATH``, "
                "or ``pip install -e`` this project from ``aiva_chatbot/`` so sibling packages resolve."
            )
            raise ImportError(msg) from exc
    _MONOREPO_PREPENDED = True


_ensure_monorepo_imports()

from embedding_service.service import EmbeddingService
from llm_service.client import LLMClient
from llm_service.config.settings import LibrarySettings
from llm_service.core.models import LLMResponse
from zoho_auth import ServiceContainer
from zoho_auth.models import ZohoUserSession
from zoho_auth.services.auth_service import AuthService


def _format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No matching documents were found in the knowledge base."
    parts: list[str] = []
    for i, h in enumerate(hits, start=1):
        text = str(h.get("text", "")).strip()
        score = h.get("score")
        score_s = f"{float(score):.4f}" if isinstance(score, int | float) else "n/a"
        parts.append(f"### Source {i} (relevance {score_s})\n{text}")
    return "\n\n".join(parts)


@dataclass
class ChatTurnResult:
    """One user message in, assistant reply plus retrieval diagnostics out."""

    text: str
    retrieval: list[dict[str, Any]]
    llm: LLMResponse
    zoho_session: ZohoUserSession | None = None


class Chatbot:
    """RAG chat: optional Zoho gate, corpus vector search, then LLM."""

    def __init__(
        self,
        *,
        embedding: EmbeddingService,
        llm: LLMClient,
        corpus_id: str,
        auth: AuthService | None = None,
        system_prompt: str | None = None,
        default_top_k: int | None = None,
    ) -> None:
        self._embedding = embedding
        self._llm = llm
        self._corpus_id = corpus_id
        self._auth = auth
        self._system_prompt = system_prompt or (
            "You are a helpful assistant. Answer using the retrieved context when it is relevant; "
            "if the context is empty or irrelevant, say so and answer from general knowledge when appropriate."
        )
        self._default_top_k = default_top_k
        self._history: list[dict[str, Any]] = []
        self._zoho_session: ZohoUserSession | None = None

    @property
    def corpus_id(self) -> str:
        return self._corpus_id

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    def authenticate(self, *, open_browser: bool = True, timeout: int = 300, force_browser: bool = False) -> ZohoUserSession | None:
        """Run Zoho OAuth when ``auth`` was provided; no-op if auth is omitted."""
        if self._auth is None:
            return None
        self._zoho_session = self._auth.login_or_resume(
            open_browser=open_browser,
            timeout=timeout,
            force_browser=force_browser,
        )
        return self._zoho_session

    async def _ensure_zoho_async(self) -> ZohoUserSession | None:
        if self._auth is None:
            return None
        if self._zoho_session is not None:
            return self._zoho_session
        loop = asyncio.get_running_loop()
        auth = self._auth
        self._zoho_session = await loop.run_in_executor(
            None,
            lambda: auth.login_or_resume(open_browser=True, timeout=300, force_browser=False),
        )
        return self._zoho_session

    def _build_messages(self, user_message: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ctx = _format_hits(hits)
        system_content = f"{self._system_prompt}\n\n## Retrieved context\n{ctx}"
        return [{"role": "system", "content": system_content}, *self._history, {"role": "user", "content": user_message}]

    def _messages_with_rag_system(self, conversation: list[dict[str, Any]], hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ctx = _format_hits(hits)
        system_content = f"{self._system_prompt}\n\n## Retrieved context\n{ctx}"
        return [{"role": "system", "content": system_content}, *conversation]

    @staticmethod
    def _strip_to_user_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content")
            if not isinstance(content, str):
                raise ValueError("Each message must have string content.")
            out.append({"role": role, "content": content})
        return out

    @staticmethod
    def _last_user_text(conversation: list[dict[str, Any]]) -> str:
        for m in reversed(conversation):
            if m.get("role") == "user":
                t = str(m.get("content", "")).strip()
                if t:
                    return t
        raise ValueError("The last message must be from the user with non-empty content.")

    async def achat_turn(
        self,
        user_message: str,
        *,
        top_k: int | None = None,
        vertical: str | None = None,
        interaction_type: str | None = None,
        issue_type: str | None = None,
        escalation: str | None = None,
        skip_retrieval: bool = False,
        **llm_kwargs: Any,
    ) -> ChatTurnResult:
        zoho = await self._ensure_zoho_async()
        tk = top_k if top_k is not None else self._default_top_k
        if skip_retrieval:
            hits: list[dict[str, Any]] = []
        else:
            loop = asyncio.get_running_loop()
            kw: dict[str, Any] = {}
            if tk is not None:
                kw["top_k"] = tk
            if vertical is not None:
                kw["vertical"] = vertical
            if interaction_type is not None:
                kw["interaction_type"] = interaction_type
            if issue_type is not None:
                kw["issue_type"] = issue_type
            if escalation is not None:
                kw["escalation"] = escalation
            hits = await loop.run_in_executor(
                None,
                lambda: self._embedding.search(self._corpus_id, user_message, **kw),
            )
        messages = self._build_messages(user_message, hits)
        llm_resp = await self._llm.achat(messages, **llm_kwargs)
        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": llm_resp.content})
        return ChatTurnResult(text=llm_resp.content, retrieval=hits, llm=llm_resp, zoho_session=zoho)

    def chat_turn(
        self,
        user_message: str,
        *,
        top_k: int | None = None,
        vertical: str | None = None,
        interaction_type: str | None = None,
        issue_type: str | None = None,
        escalation: str | None = None,
        skip_retrieval: bool = False,
        **llm_kwargs: Any,
    ) -> ChatTurnResult:
        """Synchronous turn; do not call from inside a running asyncio loop (use ``achat_turn``)."""
        if self._auth is not None and self._zoho_session is None:
            self.authenticate()
        tk = top_k if top_k is not None else self._default_top_k
        if skip_retrieval:
            hits = []
        else:
            kw: dict[str, Any] = {}
            if tk is not None:
                kw["top_k"] = tk
            if vertical is not None:
                kw["vertical"] = vertical
            if interaction_type is not None:
                kw["interaction_type"] = interaction_type
            if issue_type is not None:
                kw["issue_type"] = issue_type
            if escalation is not None:
                kw["escalation"] = escalation
            hits = self._embedding.search(self._corpus_id, user_message, **kw)
        messages = self._build_messages(user_message, hits)
        llm_resp = self._llm.chat(messages, **llm_kwargs)
        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": llm_resp.content})
        return ChatTurnResult(text=llm_resp.content, retrieval=hits, llm=llm_resp, zoho_session=self._zoho_session)

    async def achat_with_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        corpus_id: str | None = None,
        top_k: int | None = None,
        vertical: str | None = None,
        interaction_type: str | None = None,
        issue_type: str | None = None,
        escalation: str | None = None,
        skip_retrieval: bool = False,
        **llm_kwargs: Any,
    ) -> ChatTurnResult:
        """Stateless chat for HTTP APIs: ``messages`` is full user/assistant history, ending with the latest user turn."""
        zoho = await self._ensure_zoho_async()
        conversation = self._strip_to_user_assistant(messages)
        if not conversation:
            raise ValueError("At least one user or assistant message is required.")
        query = self._last_user_text(conversation)
        cid = corpus_id or self._corpus_id
        tk = top_k if top_k is not None else self._default_top_k
        if skip_retrieval:
            hits: list[dict[str, Any]] = []
        else:
            loop = asyncio.get_running_loop()
            kw: dict[str, Any] = {}
            if tk is not None:
                kw["top_k"] = tk
            if vertical is not None:
                kw["vertical"] = vertical
            if interaction_type is not None:
                kw["interaction_type"] = interaction_type
            if issue_type is not None:
                kw["issue_type"] = issue_type
            if escalation is not None:
                kw["escalation"] = escalation
            hits = await loop.run_in_executor(
                None,
                lambda: self._embedding.search(cid, query, **kw),
            )
        llm_messages = self._messages_with_rag_system(conversation, hits)
        llm_resp = await self._llm.achat(llm_messages, **llm_kwargs)
        return ChatTurnResult(text=llm_resp.content, retrieval=hits, llm=llm_resp, zoho_session=zoho)

    def close(self) -> None:
        self._embedding.close()
        self._llm.close()


def create_chatbot_from_env(
    corpus_id: str,
    *,
    use_auth: bool = True,
    system_prompt: str | None = None,
    default_top_k: int | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> Chatbot:
    """Wire default services from environment (same env vars as the three libraries)."""
    from aiva_chatbot.env_bootstrap import preload_monorepo_dotenv

    preload_monorepo_dotenv()
    settings = LibrarySettings()
    provider = llm_provider or settings.default_provider
    llm = LLMClient(provider, model=llm_model)
    embedding = EmbeddingService()
    auth: AuthService | None = ServiceContainer().auth_service if use_auth else None
    return Chatbot(
        embedding=embedding,
        llm=llm,
        corpus_id=corpus_id,
        auth=auth,
        system_prompt=system_prompt,
        default_top_k=default_top_k,
    )
