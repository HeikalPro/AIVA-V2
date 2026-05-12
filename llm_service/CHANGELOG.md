# Changelog

## 0.1.2

- **`awarmup()` / `warm()`:** pre-connect (TLS + pool) via lightweight GET; **`LLMRouter.awarmup`** warms the first provider.
- TTFT tuning: prefer **`gpt-4o-mini`** (or similar) + **`await client.awarmup()`** before streaming.

## 0.1.1

- **Latency:** `LLMClient.chat()` reuses one event loop + httpx keep-alive across calls; larger connection pool; optional HTTP/2 via `LLM_HTTP2=1` (requires `httpx[h2]`).
- **Retries:** `max_retries` from provider config is honored; default lowered to `1` for faster failures (set `OPENAI_MAX_RETRIES=3` etc. to restore multi-attempt retries).
- **Cleanup:** `LLMClient.close()`, context manager, and `aclose()` on providers / router.

## 0.1.0

- Initial release: core abstractions, HTTP-based provider adapters, middleware chain, routing, observability hooks, CLI skeleton.