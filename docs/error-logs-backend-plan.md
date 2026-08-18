# Error Logs — Backend Plan (Phase 2)

## Status

**Phase 1 (done):** A frontend-only **Error logs** tab on the Logs page
(`AIVA-V2-UI`). It composes the failures already persisted today — `FAILED`
AI requests, `FAILED` RAG retrievals, and HTTP 4xx/5xx responses — into one
filterable table (severity + source + search), with a detail dialog.

- Hook: `src/hooks/useErrorLogs.ts`
- Panel: `src/components/logs/ErrorLogsPanel.tsx`
- Tab wired in `src/pages/LogsPage.tsx` (`errors` tab, gated on `canSeeAiLogs`
  = super admin / org admin / developer).

Phase 1 ships value with **zero backend or DB change**, but three requested
fields are not in the data model yet and render as "—":

| Field | Phase 1 behavior | Gap |
| --- | --- | --- |
| Exception | ✅ `error_message` / HTTP summary | — |
| Conversation ID | ✅ `session_id` | — |
| API endpoint | ✅ HTTP `method + route` | not applicable to AI/RAG rows |
| Model | ✅ AI `model_name` | — |
| Timestamp | 🟡 RAG + HTTP only | **AI requests have no exposed timestamp** |
| User | 🟡 HTTP only | **AI/RAG rows resolve user only via session join** |
| Severity | 🟡 derived (HTTP 5xx→critical, 4xx→warning, FAILED→error) | not a stored, authoritative value |
| **Stack trace** | ❌ "Not captured yet" | not captured anywhere |
| **Retry status** | ❌ "Not captured yet" | not captured anywhere |

Phase 2 closes those gaps. It is deliberately split into small, independently
shippable steps so each lands without a big-bang migration.

---

## Step 1 — Expose timestamp + user on AI requests (small) — ✅ DONE

Shipped: `ensure_ai_request_schema` now adds `created_at` (nullable ADD, then
`MODIFY … DEFAULT SYSTIMESTAMP` so future inserts stamp themselves without
rewriting historical rows); `list_ai_requests` selects `ar.created_at` and
joins `AIVA_users` via `cs.user_id` for `user_email`; both are on `AiRequestOut`
and the `AiRequest` FE type; `useErrorLogs::mapAiRequest` maps them to
`when`/`user`. Rows logged before the migration have `when = null` and sort
last. **Requires a backend restart** (schema-ensure runs at startup).

Original notes:

The list query in `backend/services/log_queries.py::list_ai_requests` orders by
`ar.id DESC` and never selects a timestamp; `AiRequestOut`
(`backend/schemas/logs.py`) has no `created_at`/user field.

1. **Verify the column.** `DESCRIBE AIVA_ai_requests` (or query
   `user_tab_cols`). The table is pre-existing (see
   `services/ai_request_schema.py`, which only *adds* `error_message`,
   `account_id`, `source`). If there is no timestamp column, add one via the
   same idempotent `ensure_ai_request_schema` pattern:
   `ALTER TABLE AIVA_ai_requests ADD (created_at TIMESTAMP DEFAULT SYSTIMESTAMP)`.
2. **Select it + join the user.** `list_ai_requests` already
   `LEFT JOIN AIVA_chat_sessions cs`. Add
   `LEFT JOIN AIVA_users u ON u.id = cs.user_id` and select
   `ar.created_at`, `u.email AS user_email`.
3. **Schema + FE types.** Add `created_at` and `user_email` to `AiRequestOut`
   and to `AiRequest` in `AIVA-V2-UI/src/types/api.ts`.
4. **Frontend.** In `useErrorLogs.ts::mapAiRequest`, set
   `when: r.created_at` and `user: r.user_email`. Remove the "AI rows sort
   last" caveat.

**Also do the same user join for RAG** (`list_rag_retrievals` →
`chat_sessions` → `users`) so RAG error rows show a user.

## Step 2 — Server-side error filtering for HTTP logs (small)

Today `useApiLogs` pulls up to 500 rows and filters `status_code >= 400`
client-side — most rows are 2xx, so error coverage is capped by the page size.

- Add a `min_status` (or `status_class`) query param to `GET /logs/api`
  (`backend/routers/logs.py::api_logs`) and to
  `list_http_request_logs`, filtering `status_code >= :min_status` in SQL.
- Point `useErrorLogs` at it (`useApiLogs({ min_status: 400 })`) and drop the
  client-side filter.

## Step 3 — Real severity (medium)

Replace the derived severity with a stored, authoritative one.

- Add `severity VARCHAR2(16)` to each failure source (`AIVA_ai_requests`,
  `AIVA_rag_retrievals`) via idempotent `ALTER … ADD`.
- Populate at write time in the persistence paths
  (`services/ai_request_log.py`, `services/rag_retrieval_log.py`, and the
  inline INSERTs in `routers/chat.py`). Map exception class → severity
  (e.g. timeouts/5xx upstream → `critical`, validation/user-input → `warning`,
  provider errors → `error`).
- Keep the HTTP status → severity mapping (it is already authoritative from the
  status code).
- Surface `severity` in the out-schemas; have `useErrorLogs` prefer the stored
  value and fall back to the current derivation when null (older rows).

## Step 4 — Exception type + stack trace (medium/large)

This is the core new capability: capture tracebacks centrally.

1. **New table** (idempotent create, matching the `ensure_*_schema` pattern):

   ```sql
   CREATE TABLE AIVA_error_logs (
     id              NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
     created_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
     severity        VARCHAR2(16),        -- info | warning | error | critical
     exception_type  VARCHAR2(255),       -- e.g. TimeoutError
     exception_msg   VARCHAR2(2000),
     stack_trace     CLOB,                -- full traceback
     source          VARCHAR2(16),        -- AI | RAG | HTTP | APP
     endpoint        VARCHAR2(512),       -- method + route for HTTP/app errors
     status_code     NUMBER,
     model_name      VARCHAR2(255),
     session_id      NUMBER,              -- conversation id
     user_id         NUMBER,
     user_email      VARCHAR2(320),
     account_id      NUMBER,
     org_id          NUMBER,
     retry_count     NUMBER,
     retry_status    VARCHAR2(32)         -- see Step 5
   );
   ```

2. **Global exception handler / middleware.** Extend
   `backend/middleware/request_logging.py` (or add a FastAPI
   `add_exception_handler`) so unhandled exceptions — and handled 5xx — write an
   `AIVA_error_logs` row with `traceback.format_exc()`, the resolved actor (the
   middleware already builds `RequestActor`), route template, and status code.
   Keep it **best-effort** (never let logging raise), consistent with
   `persist_ai_request` / `persist_http_request_log`.

3. **AI/RAG capture.** In the `except` branches that currently write
   `status='FAILED'`, also capture `type(exc).__name__` and the traceback into
   `AIVA_error_logs` (or add `exception_type` + `stack_trace CLOB` columns to
   the existing tables — a table is cleaner for cross-source unification).

4. **Endpoint + hook.** Add `GET /logs/errors` returning the unified rows
   (org-scoped like the other log endpoints, `_AI_ACCESS`). Then simplify
   `useErrorLogs` to read this single endpoint instead of merging three — the
   merge logic stays as a fallback until the table is backfilled.

## Step 5 — Retry status (medium)

Retries happen in the LLM call path (`llm_service`) and/or the chat handler.

- Thread a `retry_count` / final `retry_status`
  (`succeeded_after_retry` | `exhausted` | `no_retry`) out of the retry loop.
- Persist it on the AI-request row and/or the `AIVA_error_logs` row.
- Surface in `AiRequestOut` / the errors endpoint; set
  `retryStatus` in `useErrorLogs` and drop the "Not captured yet" placeholder
  in `ErrorLogsPanel`.

---

## Suggested order

1. Step 1 (timestamp + user) — biggest UX win, smallest change.
2. Step 2 (server-side HTTP filter) — correctness of coverage.
3. Step 4 (stack traces) — the headline capability.
4. Step 3 (stored severity) — can ride alongside Step 4.
5. Step 5 (retry status) — last; needs LLM-path changes.

Each step is backward-compatible: the frontend already tolerates `null` for
every phase-2 field, so backend steps can ship one at a time without a
coordinated frontend release.
