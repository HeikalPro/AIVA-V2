#!/usr/bin/env python3
"""
Embedding Service – options reference + live smoke test.

Run from the project root:
    python embedding_service/demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sure the project root is importable regardless of how this script is invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

W = 68


def section(title: str) -> None:
    print(f"\n{'─' * W}\n  {title}\n{'─' * W}")


def row(label: str, value: object) -> None:
    print(f"    {label:<32} {value}")


def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")


def err(msg: str) -> None:
    print(f"  [!!]  {msg}", file=sys.stderr)


# ── 1. ALL CONFIG OPTIONS ──────────────────────────────────────────────────────

section("EMBEDDER TYPES  (CorpusConfig → embedder.type)")

print("""
  type = "http"   ──  OpenAI-compatible REST endpoint
    model              e.g. "text-embedding-3-small", "text-embedding-3-large"
    dimension          e.g. 1536  (must match the model's output)
    base_url           default: https://api.openai.com/v1  (override for vLLM / TEI)
    api_key            inline key (prefer env var instead)
    api_key_env        env var to read key from (default: OPENAI_API_KEY)
    pricing_usd_per_million_tokens   optional, for cost estimates

  type = "oracle"  ──  Oracle in-DB ONNX model via VECTOR_EMBEDDING()
    model              must match the registered ONNX name in Oracle:
                         • all_minilm_l12_v2   (dimension 384)
                         • all_mpnet_base_v2   (dimension 768)
    dimension          must match the model's output dimension
""")

section("ADAPTER TYPES  (CorpusConfig → adapter)")

print("""
  adapter = "generic_jsonl_v1"  ──  any JSONL, one object per line
    Each line must be valid JSON with at least an id and a text field.
    Example line:
      {"id": "doc-1", "text": "Hello world"}
    Config fields:
      generic_id_field    dot-path to id  (default: "id")
      generic_text_field  dot-path to text  (default: "text")

  adapter = "halan_records_v1"  ──  Halan Q&A canonical format
    Expected shape per line:
      {
        "canonical": {
          "id": 1,
          "category": {"vertical": "…", "interaction_type": "…",
                        "issue_type": "…", "escalation": "…"},
          "question": {"en": "…", "ar": "…", "notes": "…"},
          "answer":   {"text_ocr": "…", "status": "ok"}
        },
        "raw_row": {"q_body": "…"}
      }
    Supports search filters: vertical, interaction_type, issue_type, escalation
""")

section("DISTANCE METRICS  (CorpusConfig → distance_metric)")

print("""
  COSINE      normalised dot product – best default for text embeddings
  EUCLIDEAN   L2 distance (smaller = more similar)
  DOT         raw dot product – use with pre-normalised vectors
""")

section("CHUNKING  (CorpusConfig)")

print("""
  chunk_max_chars   max characters per chunk         (default: 2000)
  chunk_overlap     overlap between chunks            (default: 200)
  chunker_version   reserved for future upgrades     (default: "1")
  passage_prefix_template   text prepended to each chunk (halan_records_v1 only)
""")

section("EmbeddingService API")

print("""
  svc = EmbeddingService()           connect (uses settings from .env)

  svc.list_corpora()                 → list[dict]
  svc.get_corpus(corpus_id)          → dict | None
  svc.create_corpus(name, slug, config)   → dict
  svc.patch_corpus(corpus_id, *, name, config)  → dict

  svc.ingest(corpus_id, *, lines, records)       → {"job_id", "mode"}
  svc.reindex(corpus_id, *, lines, records, config_patch)  → {"job_id", "mode"}
  svc.get_job(job_id)                → dict  (status: QUEUED/RUNNING/DONE/FAILED)

  svc.search(corpus_id, query, *,
             top_k, vertical, interaction_type,
             issue_type, escalation)  → list[dict]

  svc.close()                        release pool
""")

# ── 2. CURRENT SETTINGS ────────────────────────────────────────────────────────

section("CURRENT SETTINGS  (from .env)")

try:
    from embedding_service.config import get_settings
    s = get_settings()
    row("oracle_dsn", s.oracle_dsn)
    row("oracle_user", s.oracle_user)
    row("oracle_password", "***")
    row("pool_min / pool_max", f"{s.pool_min} / {s.pool_max}")
    row("api_host:api_port", f"{s.api_host}:{s.api_port}")
    row("admin_api_key", "set" if s.admin_api_key else "(none)")
    row("redis_url", s.redis_url or "(none – inline mode)")
    row("redis_job_queue", s.redis_job_queue)
    row("default_openai_api_key", "set" if s.default_openai_api_key else "(none)")
    row("ingest_verbose_log", s.ingest_verbose_log)
    row("search_default_top_k", s.search_default_top_k)
    row("search_max_top_k", s.search_max_top_k)
except Exception as exc:
    err(f"Could not load settings: {exc}")
    sys.exit(1)

# ── 3. LIVE SMOKE TESTS ────────────────────────────────────────────────────────

section("LIVE TEST 1 – connect + list corpora")

try:
    from embedding_service.service import EmbeddingService
    svc = EmbeddingService()
    corpora = svc.list_corpora()
    ok(f"Connected. Found {len(corpora)} corpus/corpora.")
    for c in corpora:
        row(c["corpus_id"][:16] + "…", f"{c['name']}  (slug: {c['slug']})")
except Exception as exc:
    err(f"Connection failed: {exc}")
    sys.exit(1)

# ── create a throw-away test corpus ───────────────────────────────────────────

TEST_SLUG = "demo-smoke-test"
TEST_CORPUS_ID: str | None = None

section("LIVE TEST 2 – create corpus  (generic_jsonl_v1 + http embedder)")

try:
    existing = next((c for c in svc.list_corpora() if c["slug"] == TEST_SLUG), None)
    if existing:
        TEST_CORPUS_ID = existing["corpus_id"]
        ok(f"Corpus already exists → reusing  {TEST_CORPUS_ID}")
    else:
        corpus = svc.create_corpus(
            name="Demo Smoke Test",
            slug=TEST_SLUG,
            config={
                "adapter": "generic_jsonl_v1",
                "embedder": {
                    "type": "http",
                    "model": "text-embedding-3-small",
                    "dimension": 1536,
                },
                "distance_metric": "COSINE",
                "chunk_max_chars": 2000,
                "chunk_overlap": 200,
            },
        )
        TEST_CORPUS_ID = corpus["corpus_id"]
        ok(f"Created corpus  {TEST_CORPUS_ID}")
except Exception as exc:
    err(f"Create corpus failed: {exc}")

# ── ingest ─────────────────────────────────────────────────────────────────────

SAMPLE_RECORDS = [
    {"id": "r1", "text": "To reset your password go to Settings → Security → Reset Password."},
    {"id": "r2", "text": "Cancelling a subscription: Billing → Manage Plan → Cancel."},
    {"id": "r3", "text": "Standard shipping within Egypt takes 3–5 business days."},
    {"id": "r4", "text": "Refunds are processed within 7 working days back to the original payment method."},
]

section("LIVE TEST 3 – ingest sample records")

if TEST_CORPUS_ID:
    try:
        lines = [json.dumps(r) for r in SAMPLE_RECORDS]
        result = svc.ingest(TEST_CORPUS_ID, lines=lines)
        ok(f"Ingest dispatched  job_id={result['job_id']}  mode={result['mode']}")
        job = svc.get_job(result["job_id"])
        row("job status", job.get("status"))
        row("rows ingested", job.get("rows_ingested", "n/a"))
        row("error", job.get("error_msg") or "—")
    except Exception as exc:
        err(f"Ingest failed: {exc}")
else:
    err("Skipped – no corpus.")

# ── search ─────────────────────────────────────────────────────────────────────

section("LIVE TEST 4 – semantic search")

QUERIES = [
    "How do I change my password?",
    "I want to cancel my plan",
    "When will my order arrive?",
]

if TEST_CORPUS_ID:
    for query in QUERIES:
        try:
            hits = svc.search(TEST_CORPUS_ID, query, top_k=2)
            print(f"\n  Query: \"{query}\"")
            if hits:
                for i, h in enumerate(hits, 1):
                    score = h.get("score", h.get("distance", "?"))
                    snippet = (h.get("chunk_text") or h.get("text") or "")[:80]
                    print(f"    {i}. score={score:.4f}  {snippet}")
            else:
                print("    (no results)")
        except Exception as exc:
            err(f"Search failed for '{query}': {exc}")
else:
    err("Skipped – no corpus.")

# ── done ───────────────────────────────────────────────────────────────────────

section("DONE")
ok("All tests finished. Corpus slug 'demo-smoke-test' left in DB for re-runs.")
print()

try:
    svc.close()
except Exception:
    pass
