"""Embed chunk texts with the corpus embedder, with retries and admin-readable failures.

BLOCKING: run via ``asyncio.to_thread``. HTTP embedders are called with ``conn=None``,
so no KB connection is held while waiting on the provider. An in-database (``oracle``)
embedder needs a connection; it gets a short-lived one per batch from ``conn_factory``.
Vectors stay in memory until the publishing stage writes them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import httpx
import oracledb

from embedding_service.embedders.oracle_indb import OracleInDbEmbedder

_log = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_KEY_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"',]+"),
    re.compile(r"\b(?:sk|pk|rk|key)-[A-Za-z0-9_\-*.]{4,}"),
)


class EmbeddingFailed(Exception):
    """Embedding could not complete; ``reason`` is shown to the admin as-is (never a key)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EmbeddingOutcome:
    vectors: list[list[float]]
    api_tokens: int | None  # provider-reported usage; None when no batch reported any
    estimated_tokens: int  # ~4 chars/token heuristic, always available
    batches: int

    @property
    def tokens(self) -> int:
        return self.api_tokens if self.api_tokens is not None else self.estimated_tokens


def embed_texts(
    texts: Sequence[str],
    *,
    embedder: Any,
    batch_size: int,
    max_retries: int,
    conn_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EmbeddingOutcome:
    """Embed ``texts`` in batches; raise ``EmbeddingFailed`` with a readable reason.

    Retries (exponential backoff, ``Retry-After`` honoured) on HTTP 429 and 5xx,
    timeouts and transport errors; other client errors fail at once. Every vector
    must have the embedder's configured dimension.
    """
    items = list(texts)
    if not items:
        return EmbeddingOutcome(vectors=[], api_tokens=None, estimated_tokens=0, batches=0)
    batch_size = max(1, int(batch_size))
    max_retries = max(0, int(max_retries))
    dimension = int(getattr(embedder, "dimension", 0) or 0)
    needs_conn = _needs_connection(embedder)
    if needs_conn and conn_factory is None:
        raise EmbeddingFailed("The corpus embedder runs inside the database, but no knowledge-base connection is available")
    secret = getattr(embedder, "_api_key", None)

    vectors: list[list[float]] = []
    api_tokens: int | None = None
    estimated = 0
    batches = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        result = _embed_with_retry(embedder, batch, needs_conn, conn_factory, max_retries, sleep, secret)
        batches += 1
        got = list(getattr(result, "vectors", None) or [])
        if len(got) != len(batch):
            raise EmbeddingFailed(f"Embedding provider returned {len(got)} vectors for {len(batch)} texts")
        for vec in got:
            vec = list(vec)
            if dimension and len(vec) != dimension:
                raise EmbeddingFailed(f"Vector size {len(vec)} ≠ configured dimension {dimension}")
            vectors.append(vec)
        estimated += int(getattr(result, "estimated_tokens", 0) or 0)
        reported = getattr(result, "api_total_tokens", None)
        if reported is not None:
            api_tokens = (api_tokens or 0) + int(reported)
    return EmbeddingOutcome(vectors=vectors, api_tokens=api_tokens, estimated_tokens=estimated, batches=batches)


def _needs_connection(embedder: Any) -> bool:
    return isinstance(embedder, OracleInDbEmbedder) or bool(getattr(embedder, "requires_db_connection", False))


def _embed_with_retry(
    embedder: Any,
    batch: list[str],
    needs_conn: bool,
    conn_factory: Callable[[], AbstractContextManager[Any]] | None,
    max_retries: int,
    sleep: Callable[[float], None],
    secret: str | None,
) -> Any:
    attempt = 0
    while True:
        try:
            if needs_conn and conn_factory is not None:
                with conn_factory() as conn:
                    return embedder.embed(batch, conn)
            return embedder.embed(batch, None)
        except httpx.HTTPStatusError as ex:
            code = ex.response.status_code
            if (code == 429 or code >= 500) and attempt < max_retries:
                delay = _backoff(attempt, ex.response)
                _log.warning("doc_intel: embedding HTTP %s, retry %s/%s in %.1fs", code, attempt + 1, max_retries, delay)
                sleep(delay)
                attempt += 1
                continue
            raise EmbeddingFailed(_status_reason(code, attempt, ex.response, secret)) from ex
        except httpx.TimeoutException as ex:
            if attempt < max_retries:
                delay = _backoff(attempt, None)
                _log.warning("doc_intel: embedding timed out, retry %s/%s in %.1fs", attempt + 1, max_retries, delay)
                sleep(delay)
                attempt += 1
                continue
            raise EmbeddingFailed(f"Embedding request timed out{_after(attempt)}") from ex
        except httpx.TransportError as ex:
            if attempt < max_retries:
                delay = _backoff(attempt, None)
                _log.warning("doc_intel: embedding transport error (%s), retry %s/%s", type(ex).__name__, attempt + 1, max_retries)
                sleep(delay)
                attempt += 1
                continue
            detail = scrub_secrets(str(ex), secret)[:200] or type(ex).__name__
            raise EmbeddingFailed(f"Cannot reach the embedding endpoint ({detail}){_after(attempt)}") from ex
        except oracledb.DatabaseError as ex:
            raise EmbeddingFailed(f"In-database embedding failed: {_first_line(ex)}") from ex
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as ex:
            # Malformed provider response (checked before ValueError: JSONDecodeError is one).
            raise EmbeddingFailed(f"Embedding provider returned an unexpected response ({type(ex).__name__})") from ex
        except ValueError as ex:
            text = str(ex)
            if "api_key" in text or "API_KEY" in text:
                raise EmbeddingFailed(
                    "No API key is configured for the corpus embedder — set the variable named in "
                    "embedder.api_key_env (e.g. SOVEREIGNEG_API_KEY)"
                ) from ex
            raise EmbeddingFailed(f"Embedding failed: {scrub_secrets(text, secret)[:300]}") from ex


def _backoff(attempt: int, response: httpx.Response | None) -> float:
    delay = min(_MAX_BACKOFF_SECONDS, 2.0**attempt)
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                delay = min(_MAX_RETRY_AFTER_SECONDS, max(delay, float(raw)))
            except ValueError:
                pass
    return delay


def _after(retries: int) -> str:
    if retries <= 0:
        return ""
    return " after 1 retry" if retries == 1 else f" after {retries} retries"


def _status_reason(code: int, retries: int, response: httpx.Response, secret: str | None) -> str:
    if code == 401:
        return "Embedding provider rejected the API key (401) — check the corpus embedder key (e.g. SOVEREIGNEG_API_KEY)"
    if code == 403:
        return "Embedding provider refused access (403) — check that the API key may use the embedding model"
    if code == 404:
        return "Embedding endpoint or model not found (404) — check the corpus embedder base_url and model"
    if code == 429:
        return f"Embedding provider rate-limited the request (429){_after(retries)}"
    if code >= 500:
        return f"Embedding endpoint unavailable ({code}){_after(retries)}"
    detail = _provider_message(response, secret)
    return f"Embedding provider rejected the request ({code})" + (f": {detail}" if detail else "")


def _provider_message(response: httpx.Response, secret: str | None) -> str | None:
    """The provider's own error text (OpenAI-style ``{"error": {"message": ...}}``), scrubbed."""
    try:
        body = response.json()
    except Exception:
        return None
    message: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        message = err.get("message") if isinstance(err, dict) else (err or body.get("message") or body.get("detail"))
    if not message:
        return None
    return scrub_secrets(str(message), secret)[:200] or None


def scrub_secrets(text: str, secret: str | None = None) -> str:
    """Remove an API key (the known one, bearer tokens, ``sk-…`` style keys) from ``text``."""
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _KEY_PATTERNS[0].sub(r"\1[redacted]", text)
    return _KEY_PATTERNS[1].sub("[redacted]", text)


def _first_line(ex: BaseException) -> str:
    text = str(ex).strip()
    return (text.splitlines()[0] if text else type(ex).__name__)[:300]
