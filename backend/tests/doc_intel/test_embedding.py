"""embed_texts: batching, retries, readable failures, dimension checks, no key leaks."""
from __future__ import annotations

from contextlib import contextmanager

import httpx
import oracledb
import pytest

from backend.doc_intel.embedding import EmbeddingFailed, embed_texts, scrub_secrets
from embedding_service.embedders.result import EmbedBatchResult

from .conftest import FakeEmbedder

SECRET = "sk-live-SECRET-key-0123456789"
URL = "https://embed.example.test/v1/embeddings"


def http_error(status: int, *, body: dict | None = None, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", URL)
    response = httpx.Response(status, json=body or {"error": {"message": "nope"}}, headers=headers, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def run(texts, embedder, **kw):
    sleeps: list[float] = []
    args = dict(embedder=embedder, batch_size=2, max_retries=3, sleep=sleeps.append)
    args.update(kw)
    return embed_texts(texts, **args), sleeps


def test_batches_and_token_accounting():
    embedder = FakeEmbedder(dimension=64)
    outcome, sleeps = run(["aaaa", "bbbbbbbb", "cc", "dddd", "e"], embedder)
    assert outcome.batches == 3
    assert [n for n, _ in embedder.calls] == [2, 2, 1]
    assert all(conn is None for _, conn in embedder.calls)  # HTTP embedders hold no DB connection
    assert len(outcome.vectors) == 5 and all(len(v) == 64 for v in outcome.vectors)
    assert outcome.api_tokens == sum(len(t) // 4 for t in ["aaaa", "bbbbbbbb", "cc", "dddd", "e"])
    assert outcome.estimated_tokens > 0
    assert outcome.tokens == outcome.api_tokens
    assert sleeps == []


def test_tokens_fall_back_to_the_estimate_when_the_provider_reports_none():
    outcome, _ = run(["aaaa", "bbbb"], FakeEmbedder(report_tokens=False))
    assert outcome.api_tokens is None
    assert outcome.tokens == outcome.estimated_tokens == 2


def test_empty_input():
    outcome, _ = run([], FakeEmbedder())
    assert outcome.vectors == [] and outcome.batches == 0 and outcome.tokens == 0


def test_rate_limit_is_retried_with_backoff_and_retry_after():
    embedder = FakeEmbedder(errors=[http_error(429, headers={"Retry-After": "7"}), http_error(429), None])
    outcome, sleeps = run(["a"], embedder)
    assert len(outcome.vectors) == 1
    assert sleeps == [7.0, 2.0]  # Retry-After honoured, then exponential backoff


def test_rate_limit_after_all_retries():
    embedder = FakeEmbedder(errors=[http_error(429)] * 4)
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert info.value.reason == "Embedding provider rate-limited the request (429) after 3 retries"
    assert len(embedder.calls) == 4


def test_server_errors_after_all_retries():
    embedder = FakeEmbedder(errors=[http_error(503)] * 4)
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert info.value.reason == "Embedding endpoint unavailable (503) after 3 retries"


def test_unauthorized_fails_at_once_and_never_shows_the_key():
    embedder = FakeEmbedder(errors=[http_error(401, body={"error": {"message": f"Incorrect API key provided: {SECRET}"}})])
    embedder._api_key = SECRET
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert info.value.reason == (
        "Embedding provider rejected the API key (401) — check the corpus embedder key (e.g. SOVEREIGNEG_API_KEY)"
    )
    assert len(embedder.calls) == 1
    assert SECRET not in str(info.value)


def test_bad_request_shows_the_provider_message_scrubbed():
    body = {"error": {"message": f"Input too long for model (key {SECRET}, Bearer {SECRET})"}}
    embedder = FakeEmbedder(errors=[http_error(400, body=body)])
    embedder._api_key = SECRET
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert info.value.reason.startswith("Embedding provider rejected the request (400): Input too long")
    assert SECRET not in info.value.reason


def test_timeouts_are_retried_then_reported():
    embedder = FakeEmbedder(errors=[httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder, max_retries=2)
    assert info.value.reason == "Embedding request timed out after 2 retries"


def test_transport_error_then_success():
    embedder = FakeEmbedder(errors=[httpx.ConnectError("[Errno 11001] getaddrinfo failed"), None])
    outcome, sleeps = run(["a"], embedder)
    assert len(outcome.vectors) == 1 and sleeps == [1.0]


def test_transport_error_reported():
    embedder = FakeEmbedder(errors=[httpx.ConnectError("[Errno 111] Connection refused")])
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder, max_retries=0)
    assert info.value.reason == "Cannot reach the embedding endpoint ([Errno 111] Connection refused)"


def test_dimension_mismatch():
    class WrongSize(FakeEmbedder):
        def embed(self, texts, conn=None):
            return EmbedBatchResult(vectors=[[0.0] * 3072 for _ in texts])

    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], WrongSize(dimension=1536))
    assert info.value.reason == "Vector size 3072 ≠ configured dimension 1536"


def test_wrong_vector_count():
    class Short(FakeEmbedder):
        def embed(self, texts, conn=None):
            return EmbedBatchResult(vectors=[[0.0] * self.dimension])

    with pytest.raises(EmbeddingFailed) as info:
        run(["a", "b"], Short())
    assert info.value.reason == "Embedding provider returned 1 vectors for 2 texts"


def test_missing_api_key_is_explained():
    embedder = FakeEmbedder(errors=[ValueError("HTTP embedder requires api_key or OPENAI_API_KEY")])
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert "No API key is configured for the corpus embedder" in info.value.reason


def test_malformed_response():
    embedder = FakeEmbedder(errors=[KeyError("data")])
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert info.value.reason == "Embedding provider returned an unexpected response (KeyError)"


def test_in_database_embedder_gets_a_short_lived_connection_per_batch():
    opened: list[str] = []

    @contextmanager
    def conn_factory():
        opened.append("open")
        yield "conn"
        opened.append("closed")

    embedder = FakeEmbedder()
    embedder.requires_db_connection = True
    outcome, _ = run(["a", "b", "c"], embedder, conn_factory=conn_factory)
    assert outcome.batches == 2
    assert [conn for _, conn in embedder.calls] == ["conn", "conn"]
    assert opened == ["open", "closed", "open", "closed"]


def test_in_database_embedder_without_connection_factory():
    embedder = FakeEmbedder()
    embedder.requires_db_connection = True
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder)
    assert "no knowledge-base connection" in info.value.reason


def test_in_database_embedder_database_error():
    @contextmanager
    def conn_factory():
        yield "conn"

    embedder = FakeEmbedder(errors=[oracledb.DatabaseError("ORA-40284: model does not exist\nmore")])
    embedder.requires_db_connection = True
    with pytest.raises(EmbeddingFailed) as info:
        run(["a"], embedder, conn_factory=conn_factory)
    assert info.value.reason == "In-database embedding failed: ORA-40284: model does not exist"


def test_scrub_secrets():
    text = f"Authorization: Bearer {SECRET} and sk-proj-abc_DEF-123 and {SECRET}"
    cleaned = scrub_secrets(text, SECRET)
    assert SECRET not in cleaned and "sk-proj-abc" not in cleaned
    assert "[redacted]" in cleaned
