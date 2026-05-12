# Scaling and operations

## Horizontal scale

- **App processes**: Import `EmbeddingService` from your app(s); each process owns an Oracle pool. Run multiple app replicas behind a load balancer when needed; all point at the same KB schema.
- **Embed workers**: Set `REDIS_URL` so ingest/reindex jobs are pushed to Redis. Run **N** instances of `python -m embedding_service.worker` on separate hosts or containers. Each worker blocks on `BLPOP` and processes one job payload at a time; increase N for higher throughput.
- **Oracle**: Use **partitioned** `kb_chunk` (by `corpus_id`) as in `01_schema.sql` for partition pruning. After large bulk loads, build a **vector index** using `02_vector_index.sql.template` (set `VECTOR_MEMORY_SIZE` per DBA guide). Match query `VECTOR_DISTANCE` metric to the index metric.

## Large payloads

- Redis job messages include full `lines` arrays; very large uploads may exceed Redis value limits. For huge corpora, split into multiple ingest calls or extend the design to store payload references (object storage) in the job row.

## Index rebuild policy

- Rebuild or **ONLINE** rebuild vector indexes after major bulk re-embeds.
- Bump `chunker_version` in corpus config when chunking rules change so new chunk rows do not collide unexpectedly with old semantics.

## Idempotency

- Chunks are keyed by `(corpus_id, external_parent_id, chunk_index, chunker_version)`. Same content hash on merge skips re-embedding; changed text clears the vector for that row until the embed phase refills it.

## Ingest job metrics (time, tokens, cost)

After each successful ingest/reindex, the service writes extended fields into `kb_ingest_job.stats_json` and logs one `INFO` line `corpus_embed_done`:

- **Time**: `parse_seconds`, `embed_seconds`, `total_seconds`
- **Records**: `lines_submitted`, `lines_parsed`, `chunks_merged`, `vectors_written`, `embedding_batches`
- **Tokens**: `tokens_from_api` (sum of provider `usage.total_tokens` when returned), `tokens_estimated` (chars heuristic fallback), `tokens_used_for_cost` (API sum if any batch reported usage, else estimated)
- **Cost**: set `EMBEDDING_DEFAULT_USD_PER_MILLION_TOKENS` in the environment and/or `embedder.pricing_usd_per_million_tokens` on the corpus JSON config. `cost_usd = (tokens_used_for_cost / 1e6) * price`. If no price is configured, `cost_usd` is null.

Open-source or self-hosted embedders often omit billing; leave price unset and rely on `tokens_estimated` for rough capacity planning only.
