from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from embedding_service.embedders.result import EmbedBatchResult
from embedding_service.models.corpus_config import EmbedderConfig
from embedding_service.util.tokens import estimate_tokens_char_heuristic

if TYPE_CHECKING:
    import oracledb

_log = logging.getLogger(__name__)


class HttpOpenAIEmbedder:
    """OpenAI-compatible `/v1/embeddings` (OpenAI, Azure OpenAI path, vLLM, TEI, etc.)."""

    def __init__(self, cfg: EmbedderConfig, api_key: str | None):
        self._cfg = cfg
        self._api_key = api_key or cfg.api_key
        self.dimension = cfg.dimension
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self._url = f"{base}/embeddings"

    def embed(self, texts: list[str], conn: oracledb.Connection | None = None) -> EmbedBatchResult:
        """``conn`` is unused for HTTP embedders; may be ``None`` so callers can release the pool first."""
        if not self._api_key:
            raise ValueError("HTTP embedder requires api_key or OPENAI_API_KEY")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self._cfg.model, "input": texts}
        if self._cfg.dimension and "text-embedding-3" in self._cfg.model:
            body["dimensions"] = self._cfg.dimension

        estimated = estimate_tokens_char_heuristic(texts)

        with httpx.Client(timeout=120.0) as client:
            r = client.post(self._url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()

        usage = data.get("usage") or {}
        api_tokens = usage.get("total_tokens")
        if api_tokens is None:
            api_tokens = usage.get("prompt_tokens")
        if api_tokens is not None:
            try:
                api_tokens = int(api_tokens)
            except (TypeError, ValueError):
                api_tokens = None

        rows = sorted(data["data"], key=lambda x: x["index"])
        out: list[list[float]] = []
        for row in rows:
            vec = row["embedding"]
            if len(vec) != self.dimension:
                _log.warning(
                    "Embedding length %s != configured dimension %s; adjust CorpusConfig.embedder.dimension",
                    len(vec),
                    self.dimension,
                )
            out.append(vec)
        return EmbedBatchResult(
            vectors=out,
            api_total_tokens=api_tokens,
            estimated_tokens=estimated,
        )
