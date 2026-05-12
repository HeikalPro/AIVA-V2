# Embedding Service

A configurable vector knowledge-base service built on **Oracle 23ai**. It ingests text documents, chunks them, embeds them into vectors, and exposes fast semantic search — all managed through a simple Python API.

---

## Features

- **Multi-corpus** — manage isolated knowledge bases (corpora) in a single Oracle schema
- **Pluggable embedders** — OpenAI-compatible HTTP APIs (OpenAI, vLLM, Azure, TEI) or Oracle in-database ONNX models
- **Flexible input formats** — generic JSONL or Halan Q&A records
- **Configurable chunking** — character-window chunking with overlap, split-on-blank-line preference
- **Idempotent ingest** — same content re-submitted skips re-embedding; changed content re-embeds only the changed chunk
- **Async job queue** — inline execution by default; Redis-backed worker queue when `REDIS_URL` is set
- **Cost tracking** — token counts and USD estimates per ingest job

---

## Requirements

| Dependency | Purpose |
|---|---|
| `oracledb` | Oracle 23ai driver + vector search |
| `pydantic` + `pydantic-settings` | Config and schema validation |
| `httpx` | HTTP client for OpenAI-compatible embedders |
| `redis` *(optional)* | Async job queue |
| `tqdm` | Progress bars during ingest |

Install:

```bash
pip install oracledb pydantic pydantic-settings httpx tqdm
pip install redis  # only needed for async worker mode
```

You also need an **Oracle 23ai** instance with the AI Vector Search feature enabled.

---

## Database Setup

Run the schema script once against your Oracle instance:

```bash
sqlplus kb_user/change_me@localhost:1521/FREEPDB1 @db/sql/01_schema.sql
```

This creates three tables:

| Table | Purpose |
|---|---|
| `kb_corpus` | Corpus metadata and JSON config |
| `kb_chunk` | Chunks with `VECTOR(1536, FLOAT32)` column, partitioned by `corpus_id` |
| `kb_ingest_job` | Job tracking (status, stats, errors) |

After large bulk loads, optionally build a vector index for faster approximate-nearest-neighbour search:

```bash
# Edit the template first: set your corpus_id and VECTOR_MEMORY_SIZE
sqlplus ... @db/sql/02_vector_index.sql.template
```

---

## Configuration

The service reads config from environment variables or `.env` files. It checks two locations in order (the service `.env` overrides the root `.env`):

1. `<project-root>/.env`
2. `embedding_service/.env`

### All variables

```dotenv
# ── Oracle ────────────────────────────────────────────────
ORACLE_DSN=localhost:1521/FREEPDB1
ORACLE_USER=kb_user
ORACLE_PASSWORD=change_me
ORACLE_WALLET_DIR=           # Optional: path to wallet for mTLS
ORACLE_WALLET_PASSWORD=      # Optional
ORACLE_CALL_TIMEOUT_MS=      # Optional: e.g. 120000 — fail fast instead of hanging

# ── Connection pool ───────────────────────────────────────
POOL_MIN=1
POOL_MAX=8

# ── API server ────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8080
ADMIN_API_KEY=               # Optional: require X-API-Key header on mutating routes

# ── Async jobs (optional) ─────────────────────────────────
REDIS_URL=                   # e.g. redis://localhost:6379/0
REDIS_JOB_QUEUE=embedding_service:jobs

# ── Embedder keys ─────────────────────────────────────────
OPENAI_API_KEY=              # Used when corpus embedder.api_key_env = "OPENAI_API_KEY"

# ── Search defaults ───────────────────────────────────────
SEARCH_DEFAULT_TOP_K=10
SEARCH_MAX_TOP_K=50

# ── Ingest tuning ─────────────────────────────────────────
INGEST_BATCH_EMBED_SIZE=64   # Chunks per HTTP embedding batch
INGEST_VERBOSE_LOG=false     # Per-line timings; disable in production

# ── Cost tracking ─────────────────────────────────────────
EMBEDDING_DEFAULT_USD_PER_MILLION_TOKENS=0.02  # Fallback when corpus price is unset
```

---

## Quick Start

```python
from embedding_service.service import EmbeddingService

svc = EmbeddingService()

# 1. Create a corpus
corpus = svc.create_corpus(
    name="Help Center",
    slug="help-center",
    config={
        "adapter": "generic_jsonl_v1",
        "embedder": {
            "type": "http",
            "model": "text-embedding-3-small",
            "dimension": 1536,
        },
        "distance_metric": "COSINE",
    },
)

# 2. Ingest documents (one JSON object per line)
svc.ingest(
    corpus["corpus_id"],
    lines=[
        '{"id": "doc-1", "text": "To reset your password go to Settings → Security → Reset Password."}',
        '{"id": "doc-2", "text": "Cancel a subscription via Billing → Manage Plan → Cancel."}',
    ],
)

# 3. Search
hits = svc.search(corpus["corpus_id"], "How do I change my password?", top_k=3)
for hit in hits:
    print(hit["score"], hit["chunk_text"])

svc.close()
```

Run the bundled smoke test to verify the full pipeline end-to-end:

```bash
python embedding_service/demo.py
```

---

## API Reference

All operations go through the `EmbeddingService` class.

### Corpus management

```python
svc.list_corpora() -> list[dict]
svc.get_corpus(corpus_id: str) -> dict | None
svc.create_corpus(name: str, slug: str, config: dict | None) -> dict
svc.patch_corpus(corpus_id: str, *, name: str | None, config: dict | None) -> dict
```

### Ingest & reindex

```python
# Append new documents to an existing corpus
svc.ingest(corpus_id, *, lines=None, records=None)
# -> {"job_id": str, "mode": "inline" | "queued"}

# Clear all chunks and rebuild from scratch (optionally update corpus config)
svc.reindex(corpus_id, *, lines=None, records=None, config_patch=None)
# -> {"job_id": str, "mode": "inline" | "queued"}
```

Pass either `lines` (list of raw JSON strings) or `records` (list of dicts).

### Job tracking

```python
svc.get_job(job_id: str) -> dict
```

Job status values: `QUEUED` → `RUNNING` → `EMBEDDING` → `COMPLETED` / `FAILED`

The `stats_json` field contains timing, record counts, token usage, and cost after completion. See [docs/SCALING.md](docs/SCALING.md#ingest-job-metrics-time-tokens-cost) for the full field list.

### Search

```python
svc.search(
    corpus_id: str,
    query: str,
    *,
    top_k: int | None = None,
    # Halan adapter filters (ignored for other adapters):
    vertical: str | None = None,
    interaction_type: str | None = None,
    issue_type: str | None = None,
    escalation: str | None = None,
) -> list[dict]
```

Each result contains: `chunk_id`, `external_parent_id`, `chunk_index`, `chunk_text`, `score`, `distance`, and adapter-specific metadata fields.

### Lifecycle

```python
svc.close()  # releases the Oracle connection pool
```

---

## Corpus Configuration

The `config` dict passed to `create_corpus` / `patch_corpus` controls the full pipeline for that corpus.

### Adapters (input format)

**`generic_jsonl_v1`** — standard JSONL, one JSON object per line

```json
{
  "adapter": "generic_jsonl_v1",
  "generic_id_field": "id",
  "generic_text_field": "text"
}
```

Field values support dot-path lookups (e.g. `"content.body"`).

**`halan_records_v1`** — Halan Q&A records with structured metadata

```json
{
  "adapter": "halan_records_v1",
  "passage_prefix_template": "Vertical: {vertical}. Type: {interaction_type}."
}
```

Metadata fields `vertical`, `interaction_type`, `issue_type`, and `escalation` are stored and can be used as search filters.

### Embedders

**`http`** — OpenAI-compatible REST API

```json
{
  "embedder": {
    "type": "http",
    "model": "text-embedding-3-small",
    "dimension": 1536,
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY",
    "pricing_usd_per_million_tokens": 0.02
  }
}
```

Override `base_url` for vLLM, Azure OpenAI, or Hugging Face TEI.

**`oracle`** — Oracle in-database ONNX model via `VECTOR_EMBEDDING()`

```json
{
  "embedder": {
    "type": "oracle",
    "model": "all_minilm_l12_v2",
    "dimension": 384
  }
}
```

Supported models: `all_minilm_l12_v2` (dim 384), `all_mpnet_base_v2` (dim 768).

### Chunking

```json
{
  "chunk_max_chars": 2000,
  "chunk_overlap": 200,
  "chunker_version": "1"
}
```

The chunker uses greedy character windows, preferring to split on blank lines (`\n\n`) then newlines. Bump `chunker_version` when changing chunking rules — existing chunk rows are not overwritten.

### Distance metric

```json
{
  "distance_metric": "COSINE"
}
```

Options: `COSINE` (default, best for text), `EUCLIDEAN` (L2), `DOT` (requires pre-normalised vectors). Must match the index metric if a vector index is built.

---

## Async Worker Mode

By default the service runs ingest jobs **inline** (synchronously in-process). To offload jobs to background workers:

1. Set `REDIS_URL` in `.env`
2. Run one or more workers:

```bash
python -m embedding_service.worker
```

Each worker blocks on Redis `BLPOP`, executes one job at a time, and writes status back to `kb_ingest_job`. Scale by running more worker processes.

---

## Project Structure

```
embedding_service/
├── service.py              # EmbeddingService — main API
├── config.py               # Pydantic Settings (reads .env)
├── worker.py               # Redis worker entry point
├── demo.py                 # Smoke test / reference usage
├── .env                    # Local config (not committed)
│
├── db/
│   ├── manager.py          # Oracle connection pool
│   ├── repository.py       # Data access layer
│   └── sql/
│       ├── 01_schema.sql                  # Create tables
│       └── 02_vector_index.sql.template   # Optional ANN index
│
├── models/
│   └── corpus_config.py    # CorpusConfig, EmbedderConfig (Pydantic)
│
├── embedders/
│   ├── http_openai.py      # OpenAI-compatible HTTP embedder
│   └── oracle_indb.py      # Oracle VECTOR_EMBEDDING() embedder
│
├── adapters/
│   ├── generic_jsonl.py    # generic_jsonl_v1 adapter
│   └── halan_records.py    # halan_records_v1 adapter
│
├── chunking/
│   └── chunker.py          # Greedy character-window chunker
│
├── services/
│   └── pipeline.py         # run_ingest_job(), run_reindex_job()
│
├── workers/
│   └── redis_queue.py      # Job push / blocking pop
│
├── util/
│   ├── tokens.py           # Token estimation heuristic
│   └── uuids.py            # UUID helpers
│
└── docs/
    └── SCALING.md          # Horizontal scaling and operations guide
```

---

## Further Reading

- [docs/SCALING.md](docs/SCALING.md) — horizontal scaling, vector index rebuild policy, idempotency details, and ingest job metrics
