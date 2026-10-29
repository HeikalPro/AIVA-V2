# Document Intelligence Layer — Analysis, Design & Implementation Plan

| | |
|---|---|
| **Status** | **Phase 1 approved** (Flow 1 knowledge import + monitoring) · Phase 2 (Flow 2 SharePoint→CRM, scheduling, weekly e-mail) pending review |
| **Date** | 2026-09-24 |
| **Repos** | `AIVA-V2` (backend, branch base `CRM-ingestion-pipeline-`), `AIVA-V2-UI` (frontend, branch base `master`) |
| **Scope** | Flow 1 manual knowledge import · Flow 2 SharePoint → CRM pipeline · monitoring · scheduled sync · weekly health email · role-locked UI |

This document is the gate before any code change. It contains the current-state analysis (§1), the implementation plan with a change register that gives every change its reason, affected files, risk level and migration impact (§2), database changes (§3), API changes (§4), UI changes (§5), the migration plan (§6), the risk assessment (§7), and the test/verification plan (§8).

---

## 0. Decisions needed before implementation

| # | Decision | Recommendation | Why | If decided otherwise |
|---|---|---|---|---|
| **D1** | Who is "Admin"? | **`SUPER_ADMIN` only.** Developer = `DEVELOPER`. | Every platform-level page (LLM configs, Roles, System health) pairs `SUPER_ADMIN` with `DEVELOPER`. Microsoft credentials are platform-wide. `ORGANIZATION_ADMIN` is a tenant admin. | Including Org Admins means per-organization sources, org-scoped account pickers, and org filters on every list. Roughly +1 day and more permission surface. |
| **D2** | Where do CRM entities go? AIVA has **no** CRM database or CRM API today (§1.6). | **An internal CRM store**: new Oracle tables, an admin read API and a review table in the UI. A `CrmSink` interface allows pushing to an external CRM later. | It is the only target that exists without new third-party access. It keeps provenance and confidence. | For an external CRM (Zoho CRM, Dynamics, Salesforce, …) we need the product, API credentials and a field mapping. Flow 2 stops at "entities stored" until then. |
| **D3** | May the CRM pipeline send document text to the LLM endpoint (SovereignEG) for classification and field extraction ("CRM Intelligence")? | **Yes, as a per-source toggle.** Pattern extraction always runs locally. | SovereignEG already processes chat and KB text. LLM extraction gives far better entities than regex rules. | "Patterns only" means no document text leaves the server, but entity quality is lower (one entity per type per document, label/value rules only). |
| **D4** | Approve the changes to **existing** files and data (§2.11, rows marked **EXISTING**). | Approve all. E8 (`rag.py` source-link skip) is optional. | Each change is additive and each is listed with its risk. | Rejected rows are dropped and their consequences are listed in the row. |
| **D5** | Test database. | **A disposable local Oracle Free container** for integration tests, plus DB-free unit/API tests. **Never** the production DB. | `AIVA-V2/.env` on this laptop points at the **production** Oracle. Starting the backend locally would run startup DDL on production. | Unit/API tests with fakes only. The new SQL is then first exercised at deploy time. |
| **D6** | How production runs (Docker compose vs. `python -m backend.main` in tmux + venv). | Answer at deploy time. It affects only the runbook (§6). | The two styles differ in which `.env` files are read, whether Redis is used, and how system packages are installed. | — |

### 0.1 Decisions taken (2026-09-24)

| # | Outcome |
|---|---|
| D1 | **`SUPER_ADMIN` only** is "Admin". `DEVELOPER` gets monitoring, logs and diagnostics. |
| D2 | **Internal CRM store** (Phase 2). |
| D3 | Per-source toggle, **default off** (Phase 2). Nothing leaves the server unless an admin enables it for a source. |
| D4 | **Phase 1 = Flow 1 + monitoring.** Flow 2, the scheduler and the weekly e-mail are Phase 2, after review. E9 (`rag.py`) is **not approved**. It stays pending, and `rag.py` is untouched. |
| D5 | Use the configured Oracle database (schema `AI_ASSISTANT`) under these rules:<br>• **Only new, isolated tables** are created.<br>• **No existing table is modified**, and no DDL runs at app startup.<br>• Changes ship as versioned **migration and rollback scripts**.<br>• Before execution, the SQL is shown, the affected objects are confirmed and schema ownership is verified.<br>• The KB publish SQL is integration-tested against isolated copies (`DI_TEST_KB_CORPUS` / `DI_TEST_KB_CHUNK`), never against `kb_corpus` / `kb_chunk`. |

### 0.2 Phases

| Phase | Scope | State |
|---|---|---|
| **1** | Migration V001 (`AIVA_kb_documents`, `AIVA_health_checks`, `AIVA_health_check_events`, `AIVA_di_schema_version`) · Flow 1 (upload → extraction → chunking → embedding → publishing) · monitoring with the six components (Microsoft and CRM shown as *not configured* until Phase 2) and on-demand checks · role-locked Document Import and Monitoring pages · tests | **Approved** |
| **2a** | Migration V002 (4 CRM tables; applied 2026-09-25) · **"SharePoint Sync"** page (Super Admin) with **Sync now** and **Test connection** buttons · encrypted Graph credentials · sync (new / changed / deleted detection) · CRM store · optional automatic schedule (`DOC_INTEL_SCHEDULER_ENABLED`, default off) · real Microsoft and CRM health checks · Developer read-only connection diagnostics | **In progress** (requested by the user on 2026-09-25) |
| **2b** | Weekly Developer e-mail · retention job · LLM "CRM intelligence" (off; needs approval before any document text goes to an LLM) | Pending |

**Phase 2a design refinements**
- **Token acquisition** uses a small httpx client-credentials call (AADSTS errors mapped to admin actions) instead of MSAL, so the backend needs no new auth dependency.
- **Folder listing** (paging, recursion, site, library and path resolution) is added to `crm-document-ingestion`'s `SharePointClient`, additive and with tests.
- **CRM extraction** runs in the same isolated child process as knowledge extraction and shares its slot, so there is never more than one extraction at a time.
- **Scheduler and run queue:** the sync-runs table is the work queue. A unique function-based index allows only one QUEUED or RUNNING run per source, which makes a double "Sync now" a 409. The scheduler claims due sources with an optimistic `UPDATE … WHERE next_sync_at = :old`, so the planned `AIVA_scheduler_jobs` / `AIVA_scheduler_runs` tables are no longer needed; the weekly e-mail adds its own in 2b.

### 0.3 Phase 1: implementation status (2026-09-25)

**Built** on local branch `feature/document-intelligence` in both repos. Nothing is committed, pushed or deployed.

| Area | Result |
|---|---|
| Backend | `backend/doc_intel/**`: 24 modules, API under `/api/doc-intel` (12 paths, 14 operations). Existing-file edits: E1, E2, E4, E6 and the ignore files. That is +57 lines over 6 existing files, all additive. |
| Frontend | 14 new files (Document Import and Monitoring pages, components, hooks). Existing-file edits: E10–E13 (4 files, +223/−4, additive). The old and new permission code were compared across 154,719 checks and are identical for every existing page and role. |
| Database | V001 applied (4 new, empty tables). The `DI_TEST_*` fixtures were created, used and **dropped** on 2026-09-25. No existing table was written to at any point. |
| Unit and API tests | `backend/tests/doc_intel`: **329 passed** |
| Oracle integration tests (opt-in, `DOC_INTEL_IT=1`) | **25 passed.** They run inside savepoints that are always rolled back, and leave 0 rows. Suite total with them: 354 passed. |
| Existing suites | `crm-document-ingestion` 123 passed · `llm_service` unit 6 passed · UI `tsc` and `vite build` clean |
| End-to-end (API) | 42/42 checks passed, both on in-memory stores and on **real Oracle** (committed writes to the doc-intel tables and the DI_TEST copies, cleaned up afterwards). The run used the real extraction, including the Arabic page-OCR fallback on `6 Digits.pdf`, the real chunking, embedding client, publish SQL and health checks. |
| End-to-end (browser) | 30/30 Playwright checks: Super Admin flow, Developer and Agent (with the locked keys granted) visibility and direct-URL blocking, dark mode, a 390 px width |

**Review findings** (Agent 5 plus the lead):

| ID | Sev | Finding | Status |
|---|---|---|---|
| F1 | **High** | *Existing code:* `POST /api/users/{id}/roles` and `POST /api/users` let an **Org Admin assign any role, including SUPER_ADMIN, to users of its own org, itself included** (`backend/routers/users.py:141-158, 440-460`). This bypasses every role guard in AIVA. | **Open, needs approval** (an existing-behaviour change) |
| F2 | Med | Vector bind over 32 KB (ORA-01461) could fail a publish | Fixed (`vector_text`, 9 significant digits) and tested on Oracle |
| F3 | Med | Health details over 32 KB broke the MERGE (ORA-03146) | Fixed and tested on Oracle |
| F4 | Med | Doc-intel error rows would have been visible to tenant org admins through `/api/logs/errors` (NULL org) | Fixed: scoped to `NOTIFY_PLATFORM_ORG_ID`, or not written at all |
| F5 | Med | Monitoring is not org-scoped. Any DEVELOPER sees every tenant's document names. This matches how DEVELOPER is treated elsewhere today. | **Decision** (restrict to platform-org developers?) |
| F6–F8 | Low | Traceback scrubbing, orphaned upload files, migration DROP scope | Fixed and tested |
| F9 | Low | The upload body was parsed before the auth guard | Fixed: parsed after the guard, plus an early 413 check |
| F12 | Low | An in-flight extraction child survived shutdown | Fixed: killed on stop |
| F13 | Low | "Run checks" could exceed nginx's 60 s | Fixed: the smoke-test default is now 40 s |
| F10, F11 | Low | Races and states between concurrent admin actions; a FAILED republish keeps the old version live | Open (follow-up) |
| F14–F16 | Low | The wheel sits in an image layer (use a BuildKit mount + hashes); the embedder probe follows the corpus URL; the child process has no memory cap | Open (hardening) |
| F17 | Info | *Existing* `embedding_service/db/repo.py` has the same latent 32 KB vector-bind limit as F2 | Open (not triggered by today's provider) |

**Still pending from the user:**
- F1;
- E9 (`rag.py` source-link skip; without it an imported document ranked first shows a broken KB link);
- F5;
- D6 (production deployment style);
- the Phase 2 review.

---

## 1. Current-state analysis

### 1.1 Architecture

- **Backend.** `AIVA-V2/backend` is FastAPI served by **one uvicorn process** with no workers (`backend/main.py:141-154`). 18 routers are mounted under `/api` (`backend/routers/__init__.py:25-45`).
  - Middleware order: SlowAPI (120 req/min/user), RequestLogging (a row per request in `AIVA_http_request_logs`, query string included; tracebacks to `AIVA_error_logs`), CORS.
  - The catch-all 500 handler returns the exception text to the client (`main.py:115-128`).
- **Lifespan** (`main.py:48-82`), in order:
  1. Async Oracle pool.
  2. Super-admin bootstrap.
  3. 13 `ensure_*_schema()` calls, sequential and with **no try/except**, so any failure aborts startup.
  4. `EmbeddingService()` with its own **sync** pool (max 8, shared with chat retrieval).
  5. The one background task: the daily SovereignEG price refresh (`services/sovereign_catalog.py:142-164`).
- **In-process libraries:** `llm_service` (chat completions) and `embedding_service` (KB ingest and vector search).
- **Out-of-process work.** Host cron runs `scripts/watchdog.py` (every 5 min) and `scripts/backup_db.py` (Sun 03:00). `scripts/` is not in the Docker image. An optional `embedding-worker` container and Redis exist in compose profile `ingestion`, but `deploy.sh` does not start them.
- **Deployment.**
  - `Dockerfile` uses `python:3.12-slim` and copies only `backend`, `embedding_service`, `llm_service` and `zoho_auth`.
  - Compose bind-mounts `./data/widget_release` and `./data/zoho`; nothing persists documents.
  - nginx allows 320 MB bodies with a 60 s proxy timeout.
  - Production also appears to run as a tmux + venv process on the host (see D6).

### 1.2 Database models and migrations

- **SQL style:** raw `oracledb` with named binds and no ORM. `Database.execute` commits per call and does not expose `rowcount` (`backend/database.py:118-136`). Multi-statement atomicity uses `async with db.connection() as conn`.
- **Migrations** are idempotent DDL at startup. Each module checks `user_tables` / `user_tab_cols` before CREATE/ALTER (e.g. `services/audit_schema.py:10-59`, `services/error_log.py:26-53`).
- **Conventions:**
  - Names `AIVA_<snake>`; `id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY`; `TIMESTAMP(6) DEFAULT SYSTIMESTAMP`.
  - JSON stored as CLOB; booleans as `NUMBER(1)`; indexes named `idx_aiva_*`.
  - No BLOBs.
- **Missing DDL:** core tables (`AIVA_accounts`, `AIVA_users`, `AIVA_roles`, `AIVA_user_roles`, `AIVA_account_users`, …) have no DDL in the repo. KB tables are created by hand from `embedding_service/db/sql/01_schema.sql`:
  - `kb_corpus(corpus_id RAW(16), config_json JSON)`.
  - `kb_chunk`: `external_parent_id VARCHAR2(64)`, `chunk_index`, `chunker_version`, `content_hash`, `chunk_text CLOB NOT NULL`, `payload_json JSON`, `embedding VECTOR(1536, FLOAT32)`, UNIQUE `(corpus_id, external_parent_id, chunk_index, chunker_version)`.
  - `kb_ingest_job`.
  - There is no vector index; search is an exact scan.
- **Two pools, one schema today.** The app pool (backend `.env`) and the KB pool (`embedding_service/.env`) currently resolve to the same schema. They are separate connections, so no transaction spans both.

### 1.3 Permissions system

- **Roles** (`backend/auth/role_constants.py`): `SUPER_ADMIN`, `ORGANIZATION_ADMIN`, `ACCOUNT_MANAGER`, `SUPERVISOR`, `AGENT`, `DEVELOPER`.
- **Guards** (`backend/auth/deps.py`):
  - `require_roles(*roles)` (153-161). `SUPER_ADMIN` always passes.
  - `require_roles_or_nav_permission(key, *roles)` (181-198) lets in **anyone holding the page key, whatever their role**.
- **Page visibility is server-driven** (`services/role_nav_permissions.py`).
  - Sources: a catalog (16-34), defaults per role (51-101), and three tables (global, per-account, per-user extras).
  - `SUPER_ADMIN` is a wildcard and receives every catalog key.
  - Org Admins can grant extra pages to any user in their org, except `ORG_ADMIN_RESTRICTED_NAV_KEYS` (46-48).
  - `/api/auth/me` returns the resolved keys and the UI trusts them.
- **Consequence for this work:** a normal page key could be granted to an agent. The new endpoints must therefore use **role-only guards**, and the new pages must be **role-locked** in the UI (§2.8).

### 1.4 Accounts, queues and the knowledge base

- **Account → KB.** An account points at one KB corpus through `AIVA_accounts.corpus_id` (hex). Several accounts may share a corpus.
- **A queue is not a table.** It is a key of `kb_corpus.config_json.queue_groups` → `{label, verticals[]}` (`backend/services/kb_queue_groups.py:34-54`). When a corpus has none, a hard-coded HALAN / Gomla / Tasaheel fallback applies (8-31).
- **Retrieval:**
  1. The session's `active_queues`, limited by `AIVA_agent_queue_access`, resolve to the union of those queues' verticals (`kb_queue_groups.py:66-84`; no length cap).
  2. The search filter is `JSON_VALUE(payload_json,'$.vertical') IN (:v…)` (`embedding_service/db/repo.py:399-410`).
  3. Config is re-read on every message, so a `queue_groups` edit takes effect on the next message.
- **Queue catalog for UIs:** `GET /api/accounts/{id}/kb-queues` (`backend/routers/accounts.py:182-207`). `SUPER_ADMIN` passes its guard.
- **Ingestion today:**
  - `POST /api/ingestion/trigger` accepts JSONL lines/records only.
  - The adapter is fixed per corpus (`halan_records_v1` or `generic_jsonl_v1`, the latter with no vertical).
  - The chunker splits by characters (2000 / 200 overlap).
  - The embed phase is **corpus-wide** (it embeds any NULL-vector row in the corpus).
  - REINDEX deletes **every** chunk of the corpus.
- **Uploads:** there is **no** upload endpoint, no document concept and no file storage. `IngestionPage.tsx` is a request-ticket board. `python-multipart` is installed but unused.
- **Precedent:** the live **6-digits** queue is exactly "one dedicated vertical mapped to one queue". It was ingested manually from `6 Digits.pdf`.

### 1.5 Existing APIs relevant to this work

| Area | Endpoints | Guard |
|---|---|---|
| System health | `GET /api/system/health`, `/system/components`, `/system/resources` — live checks, nothing persisted, no per-component timestamp | `SUPER_ADMIN`, `DEVELOPER`, or page key `system` |
| Logs | `/api/logs/{activity,sign-in,api,rag,ai-requests,ai-metrics,errors}`, `/api/http-logs` | role or page key `logs` |
| KB / ingestion | `/api/corpora`, `/api/ingestion/{requests,trigger,jobs}` | mixed; trigger has no corpus-ownership check |
| Accounts / queues | `/api/accounts`, `/api/accounts/{id}/kb-queues` | role or page key |
| Notifications | `fetch_role_emails(roles, organization_id)` and alerts (`services/notifications.py:24-137`); e-mail through `get_mail_sender()` (SMTP or Zoho Mail) and `build_message()` (`services/email/templates.py:369-388`) | — |

### 1.6 Existing CRM and document pieces

- **`crm-document-ingestion`** (in `AIVA-V2/`, added in the last commit; used by nothing yet):
  - A Microsoft Graph **single-item** client: client-credentials auth, get by drive+item or sharing URL, download, 429/503 retry.
  - A `document-extractor` adapter.
  - Generic CRM schemas (`organization`, `contact`, `document_reference`), pattern and LLM ("intelligence") extractors, a validator, and `to_crm_json()`.
  - 123 offline tests in its own `.venv`.
  - **Gaps:** no folder listing, no paging, no delta, no persistence.
  - Credentials can be passed programmatically.
- **CRM target:** **none.** `zoho_auth` is OAuth login plus a Mail token. `tickets` push to Zoho Desk. There are no Zoho CRM calls and no CRM tables.
- **`document-extractor`** (a proprietary GoChat247 library at `D:\text_extraction_and_ocr`, not on PyPI):
  - It is installed editable in `AIVA-V2/venv` only.
  - `integrations.chunking.iter_chunks()` is a heading-aware chunker that returns page numbers and section paths.
  - It is **not thread-safe** (PDFium, MuPDF and Tesseract: one thread per process).
  - Arabic OCR needs `ocr_languages=("ara","eng")` plus the Tesseract `ara` pack.
  - `ocr="always"` does not re-OCR PDF pages that have a (broken) text layer. That is why the root `test.py` renders pages and OCRs them.
  - The repo's `document_extractor.toml` turns `[intelligence]` **on** (sends text to SovereignEG), but no AIVA code reads it.
  - PyMuPDF is AGPL.

### 1.7 Current UI structure

- **Stack:** React 19, Vite 7, TypeScript (strict), Tailwind 3, TanStack Query and react-router 7. There are no frontend tests or linters.
- **Routing:** routes are generated from `NAV_ITEMS` (`src/lib/roles.ts:37-137`) plus `ROUTE_PAGES` (`src/App.tsx:28-44`), each wrapped in `ProtectedRoute(permission)`. `canAccessPermission` lets `SUPER_ADMIN` through, otherwise checks the `permissions` returned by `/me` (`roles.ts:157-165`).
- **Page-access editor:** every `NAV_ITEM` automatically appears in the per-user page-access editor (`UserExtraPageAccessEditor.tsx`). A plain new item would therefore be grantable to agents.
- **Reusable pieces:**
  - `apiUpload(path, FormData)` (`src/lib/api-client.ts:142-184`), unused so far.
  - `QueueSelector`, `DataTable`, `StatusBadge`, `PageHeader`, `Dialog`.
  - The LogsPage tab pattern.
  - `SystemComponentsSection` status tones (emerald / amber / red).
- **No secret input pattern exists** anywhere.
- **Dark mode:** `.dark` class plus remaps in `src/index.css`. New colors must use theme tokens or already-remapped shades (emerald / red / amber 50–200, 600–800; solid `*-500` dots).

---

## 2. Implementation plan

### 2.1 Principles

1. **Additive and isolated.** All backend logic lives in a new package, `backend/doc_intel/`. Existing modules are *imported*, never edited, except for the registration lines in the change register.
2. **No change to existing behaviour:**
   - no existing table is ALTERed;
   - no existing endpoint or response changes;
   - no existing role gains or loses a page;
   - chat retrieval code is untouched.
3. **Fail closed, degrade gracefully.**
   - A doc-intel schema failure disables only the module; the app still starts.
   - A missing encryption key blocks only credential operations.
   - A missing `document-extractor` fails only the extraction stage, with a clear reason.
4. **Heavy work never runs on the event loop.** Extraction runs in a **child process**, one document at a time, with a hard timeout. Embedding HTTP calls hold no DB connection.
5. **Automation is off by default:** `DOC_INTEL_SCHEDULER_ENABLED=false` and `DOC_INTEL_WEEKLY_REPORT_ENABLED=false`, matching the existing `NOTIFY_*` convention.
6. **Role-only guards** on every new endpoint (§2.8).

### 2.2 Component overview

| Component (new, `backend/doc_intel/`) | Responsibility |
|---|---|
| `settings.py` | `DocIntelSettings` (`DOC_INTEL_*` env vars), reading the same `.env` files as `backend/config.py` |
| `schema.py` | `ensure_doc_intel_schema(db)`: idempotent DDL for the 9 new tables (§3) |
| `crypto.py` | `SecretBox`: Fernet/MultiFernet encryption (key rotation), fail-closed, plus a `generate-key` CLI |
| `guards.py` | `ADMIN_ONLY = require_roles(SUPER_ADMIN)`, `ADMIN_OR_DEVELOPER = require_roles(DEVELOPER)` |
| `storage.py` | Safe file storage under `DOC_INTEL_STORAGE_DIR` (streamed writes, size caps, sanitized names, atomic rename) |
| `extraction.py` | Child-process runner around `document-extractor`: explicit options, Arabic text-layer check with page-render OCR fallback (pypdfium2, not AGPL PyMuPDF), timeout, **normalized document** JSON |
| `chunking.py` | Normalized document → chunk drafts: `iter_chunks` for structured documents, page-wise splitting for OCR output; a `[Document · Section · Page]` header on every chunk |
| `kb_publish.py` | The only code that writes KB data: an atomic publish, unpublish and queue change (chunks + vectors + `queue_groups` vertical in one KB transaction) |
| `kb_import.py` | Flow 1 state machine: DB-backed work queue, the 5 stages, retry, republish, restart recovery |
| `graph_source.py` | SharePoint access built on `crm_ingestion`: credentials from the DB, site/drive/folder resolution, paged recursive listing |
| `crm_store.py` | Internal CRM store plus the `CrmSink` interface (D2) |
| `crm_sync.py` | Flow 2 orchestration: list → diff (new / changed / deleted / unchanged) → per-file stages → run summary |
| `health.py` | Six component checks with reason, suggested action, `last_success_at` and transition events |
| `scheduler.py` | 60 s ticker with DB leases: health checks, due SharePoint syncs, weekly report, retention |
| `weekly_report.py` | Composes and sends the Sunday report to the Developer role |
| `runtime.py` | Worker lifecycle (start/stop from the lifespan) and the shared process-wide extraction slot |
| `routers/{kb_documents,integrations,monitoring}.py`, `schemas.py` | HTTP API (§4) and Pydantic models |

`crm-document-ingestion` gains additive Graph methods for folder listing (§2.4). `embedding_service` is **not modified**. Its public primitives (`make_embedder`, the chunker, repo helpers) are imported.

### 2.3 Flow 1 — Manual knowledge document import

The five stages are recorded per file. Each is `PENDING` → `RUNNING` → `COMPLETED` or `FAILED` (with reason). After a failure the later stages stay `PENDING`, or become `SKIPPED` if the upload was rejected.

| Stage | Where it runs | What happens | Typical failure reasons shown |
|---|---|---|---|
| **Upload** | HTTP request | 1. `SUPER_ADMIN` only.<br>2. Account must exist and have a corpus. Queues: ≥1, each in that corpus's queue catalog (`validate_active_queues`).<br>3. Per file: extension `.pdf`/`.docx` **and** magic bytes, ≤ `DOC_INTEL_MAX_UPLOAD_MB` (50), ≤ 20 files per request, sanitized filename.<br>4. Stream to disk while computing SHA-256, then create the `AIVA_kb_documents` row.<br>5. Returns **202** immediately (nginx has a 60 s timeout). | "Unsupported file type (only PDF and DOCX)", "File is 72 MB; limit is 50 MB", "Duplicate of document #12 (already published to these queues)", "Account has no knowledge base" |
| **Extraction** | Child process (one at a time) | 1. `document-extractor` with **explicit** options (the repo TOML is never used):<br>• `ocr_languages=("ara","eng")`<br>• `pdf_engine="pdfium"`<br>• intelligence **off**, so nothing leaves the server<br>• page cap `DOC_INTEL_MAX_PAGES` (300)<br>• timeout `DOC_INTEL_EXTRACTION_TIMEOUT_SECONDS` (900).<br>2. The Arabic text-layer quality check (ported from `test.py`) triggers page-image OCR when lam-alef pairs are reversed.<br>3. Writes `normalized.json`: pages, blocks and section paths, warnings, engine info. | "PDF is password-protected", "No text found — scanned file and OCR unavailable on the server", "Extraction timed out after 900 s", "File is corrupt: …" |
| **Chunking** | API process (pure Python) | Uses `iter_chunks` (section-aware, with page metadata) or page-wise splitting for OCR output. Size comes from corpus `chunk_max_chars`/`chunk_overlap`. Each chunk is prefixed `[Document: <file> · Section: A › B · Page 3]`, because the LLM sees chunk text only. `external_parent_id = kbdoc-<id>`, `chunker_version = docimport-1`. Empty chunks are dropped; the cap is `DOC_INTEL_MAX_CHUNKS` (2000). | "Document produced no text chunks", "Document produces 3,400 chunks; limit is 2,000" |
| **Embedding** | Worker thread (HTTP) | Uses the corpus embedder (`make_embedder`, today SovereignEG `text-embedding-3-small`). Batches of 64. **No DB connection is held.** Retries 3× on 429 / 5xx / timeout. Checks the vector dimension (1536). Vectors stay in memory; tokens and cost are recorded. | "Embedding provider rejected the API key (401)", "Embedding endpoint unavailable (503) after 3 retries", "Vector size 3072 ≠ 1536" |
| **Publishing** | One KB transaction | 1. `SELECT config_json … FOR UPDATE` on the corpus row.<br>2. Delete this document's previous chunks.<br>3. Insert chunks together with their vectors.<br>4. Add vertical `kbdoc-<id>` to each selected queue's `verticals`. If the corpus has no `queue_groups`, the current defaults are materialized first. All other config keys are preserved.<br>5. **COMMIT.** The document becomes searchable for the selected queues atomically, on the next chat message. | "Knowledge base database unavailable", "Queue 'Card Support' no longer exists" |

**How a document is made available to several queues.** Each document gets its own vertical, `kbdoc-<id>`, which is appended to each selected queue's `verticals` list.
- It needs **zero change to chat retrieval**: the existing `IN (…)` filter does the work.
- It keeps one vector set.
- Unpublishing or changing queues is a config-only edit (no re-embedding).
- It follows the live 6-digits precedent.

Alternatives were rejected:
- **Duplicate chunk set per queue:** N× cost, and duplicates crowd `top_k`.
- **A `queues` array in the payload:** changes every search's SQL.

Scaling note: the `IN` list grows by one per document per queue. That is fine into the hundreds per queue. The fallback, if ever needed, is one vertical per queue combination.

**Other operations:**

| Operation | Behaviour |
|---|---|
| **Unpublish** | Remove the vertical from every queue first (immediately invisible), then delete the chunks. The row is kept as `UNPUBLISHED`. |
| **Change queues** | Edits the config only. No re-embedding. |
| **Retry** | Resumes from the failed stage; `normalized.json` is reused. |
| **Republish** | Re-chunks, re-embeds and re-publishes from `normalized.json`. Used after an external REINDEX. |

**Queue, restart and integrity:**
- The `AIVA_kb_documents` rows *are* the work queue, claimed with a conditional `UPDATE`, so it is safe with more than one process.
- At startup, rows left `RUNNING` by a restart are marked `FAILED: interrupted by a server restart — Retry`.
- An **integrity check** runs inside the "Knowledge sync" health component. It verifies that every `PUBLISHED` document still has its chunks and its vertical in each selected queue. This catches REINDEX runs and `scripts/seed_queue_groups.py` overwrites, and offers **Republish**.

### 2.4 Flow 2 — Automated CRM document pipeline (SharePoint / OneDrive)

**Source configuration** (`AIVA_crm_sources`, `SUPER_ADMIN`). Each source has:
- a name and an optional AIVA account (scopes the entities);
- Tenant ID, Client ID and Client Secret, **all encrypted**;
- SharePoint site URL (must be `https://*.sharepoint.com/...`), library (default `Documents`), folder path, and recursive yes/no;
- file types (default `.pdf,.docx`);
- use LLM intelligence yes/no (D3);
- schedule: enabled, every 7 / 14 / 21 / 28 days or custom 1–365, and the hour of day (default 02:00 Africa/Cairo, outside business hours).

**Sync run** (scheduled or "Sync now"):
1. Decrypt credentials and build `MicrosoftGraphSettings(_env_file=None, …)` explicitly, so no env fallback leaks in. Acquire an app-only token (MSAL client credentials).
2. Resolve the site, drive and folder IDs (cached on the source row).
3. List the folder recursively with `@odata.nextLink` paging (new methods in `crm-document-ingestion`). Collect id, name, path, eTag, cTag, size, modified time, quickXorHash and webUrl. The cap is `DOC_INTEL_SYNC_MAX_FILES` (5000).
4. Diff against `AIVA_crm_source_files`. A file is **new** (unknown item), **changed** (cTag differs, falling back to eTag or quickXorHash), **deleted** (active in the DB but not listed), or **unchanged**. Previously failed, unchanged files are retried up to 3 attempts.
   - **Deletions are applied only when the listing completed.** A partial listing makes the run `PARTIAL` and nothing is marked deleted.
   - A full listing was chosen over Graph `/delta`: with a 1–4 week cadence it is simple and robust. Delta works only from the drive root on SharePoint and needs 410-resync handling. It can be added later.
5. **Deleted files** become `DELETED` and their entities `WITHDRAWN` (a soft delete; history is kept).
6. **New and changed files** are processed sequentially through five stages, all recorded on the file row:

   | Stage | What happens |
   |---|---|
   | download | Streamed, ≤ 100 MB |
   | extraction | Same child-process runner as Flow 1; intelligence only if the source toggle is on |
   | intelligence | `CRMExtractionService` running `IntelligenceExtractor` (when on) plus `PatternExtractor`, over the generic schemas plus client JSON schemas from `DOC_INTEL_CRM_SCHEMA_PATHS` |
   | entities | Validation report and confidence |
   | persist | "CRM Database/API": replaces this file's entities in one transaction and stores the full `CRMProcessingResult` JSON |

7. Close the run: counts and status are `COMPLETED`, `PARTIAL` (some files failed or the listing was truncated) or `FAILED` (auth or listing failed). Update `last_sync_*`, then set `next_sync_at = run start + interval`, aligned to the configured hour.

A manual "Sync now" also resets the clock, so the interval means "at most N days between checks".

**CRM store** (D2): `AIVA_crm_entities`, one row per entity. Each row holds:
- type, display value and a normalized match key (email, phone digits, tax id or folded name);
- confidence, validity and issues;
- the full field JSON with page and block provenance;
- status `ACTIVE` or `WITHDRAWN`.

The admin API lists and filters entities. `CrmSink` has one implementation now, `InternalOracleSink`; an external CRM push can be added later without touching the pipeline.

### 2.5 Monitoring (Admin + Developer)

Each component is stored in `AIVA_health_checks` with these fields: status, reason, `checked_at`, `last_success_at`, `last_failure_at`, `consecutive_failures`, `suggested_action`, latency and details. Status changes are appended to `AIVA_health_check_events`.

Statuses: **HEALTHY (green)**, **FAILED (red)**, **NOT_CONFIGURED (grey)**. Grey is only for things nobody has set up yet (for example no SharePoint source), so an unconfigured optional feature does not raise false alarms. It is reported as "not configured", not as a failure.

| Component | Check (every call bounded by a timeout) | Healthy when | Examples of reason → suggested action |
|---|---|---|---|
| **Microsoft connection** | For each enabled source: decrypt, get a token, then `GET` the configured folder item | All enabled sources pass | • Secret invalid or expired (`AADSTS7000215`/`7000222`) → "Create a new client secret in Entra ID → App registrations → Certificates & secrets and paste it in Integrations".<br>• App not in tenant (`AADSTS700016`) → "Check Tenant ID and Client ID".<br>• 403 → "Grant `Sites.Read.All` + `Files.Read.All` (Application) or `Sites.Selected`, then admin consent".<br>• 404 → "Check site URL, library and folder path".<br>• Network → "Allow outbound HTTPS to login.microsoftonline.com and graph.microsoft.com".<br>• Key missing → "Set `DOC_INTEL_SECRETS_KEY` and restart" |
| **CRM connection** | `CrmSink.health()`: the internal store is queryable, plus persist errors in the last run | The query succeeds and the last run had no persist failures | "CRM store unreachable — see Database" |
| **Knowledge sync** | SharePoint: last run status and overdue check (> interval + 1 day). KB imports: none stuck > 60 min, published-document integrity (§2.3), and failures in the last 24 h | All pass | • "Last sync failed: <reason> — fix, then Sync now".<br>• "Sync overdue — scheduler disabled?"<br>• "3 published documents lost their chunks (corpus re-indexed?) — Republish" |
| **Extraction service** | `document-extractor` import and version; Tesseract binary with `ara` + `eng`; a child-process smoke extraction of a tiny generated DOCX | All pass | • "document-extractor not installed in the backend environment — see runbook".<br>• "Tesseract Arabic pack missing — `apt install tesseract-ocr-ara`".<br>• "Smoke test timed out" |
| **Embedding service** | KB pool `SELECT 1`, plus `GET {embedder.base_url}/models` for each distinct corpus embedder in use (costs no tokens) | Both pass | • "Embedding endpoint rejected the key (401) — update `SOVEREIGNEG_API_KEY`".<br>• "Provider unavailable (5xx)" |
| **Database** | App pool and KB pool `SELECT 1 FROM dual` (5 s timeout each) | Both pass | • "ORA-00257 archiver error — DB host disk full (see runbook)".<br>• "Connection timeout — DB container down?" |

"Knowledge sync" is interpreted as the health of both document sync paths: SharePoint and KB import.

**When checks run:**
- every `DOC_INTEL_HEALTH_INTERVAL_MINUTES` (15) through the scheduler;
- on demand with "Run checks now" (limited to once per 30 s);
- immediately before the weekly report.

The UI reads **stored** results and polls every 30 s. Polling never triggers live Microsoft or CRM calls.

**Developer tools:**
- **Logs:** a doc-intel activity feed (stage failures, sync runs, health transitions), plus the existing Errors view. Unexpected exceptions are also written to `AIVA_error_logs` with `source='DOC_INTEL'`.
- **Connection diagnostics:** step-by-step per source (key → token → site → drive → folder → sample listing). Each step shows latency and the raw error code. Secrets never appear.

### 2.6 Scheduling

- **Ticker.** An asyncio loop started in the lifespan when `DOC_INTEL_SCHEDULER_ENABLED=true`. It ticks every 60 s and is cancelled on shutdown. This follows the `sovereign_catalog` precedent.
- **Duplicate prevention.** Each job holds a **DB lease** in `AIVA_scheduler_jobs`, via a conditional `UPDATE … WHERE lease_until < now`, accepted only if exactly one row changed. The weekly report additionally claims a **unique period row** in `AIVA_scheduler_runs`. A second backend process or replica cannot double-run.

| Job | When |
|---|---|
| `health` | Every N minutes |
| `crm_sync:<source_id>` | When the source is enabled and `next_sync_at ≤ now`. Runs on the doc-intel worker, so it never overlaps an extraction |
| `weekly_report` | Sunday ≥ `DOC_INTEL_WEEKLY_REPORT_HOUR` (09:00, after the 03:00 backup), Africa/Cairo, once per Sunday. It catches up if the server was down at that time |
| `retention` (daily) | Prunes health events > 180 days and runs > 365 days. Original uploads are kept unless `DOC_INTEL_ORIGINALS_RETENTION_DAYS` > 0 |

- **UI control.** Per source: interval (1 / 2 / 3 / 4 weeks or custom days), hour, enabled flag. Next and last run are shown.
- **Environment flags.** The global scheduler switch and the weekly-report switch stay environment flags, so a deployment opts in explicitly.

### 2.7 Weekly e-mail (every Sunday)

1. Run all health checks fresh.
2. Collect:
   - component states;
   - failed services;
   - failed documents in the last 7 days (KB imports and CRM files);
   - SharePoint sync runs (new / changed / deleted / failed);
   - connection problems (health transitions to FAILED, with duration);
   - de-duplicated recommended actions.
3. Build the message with the existing `build_message()` blocks (details table, text blocks, note, CTA to `/monitoring`). **No change to `templates.py`.**
   - Subject: `[AIVA] Weekly health report — all systems healthy`, or `[AIVA ALERT] Weekly health report — 2 services failing`.
4. Recipients: active users with the **Developer** role (`fetch_role_emails([DEVELOPER], organization_id=NOTIFY_PLATFORM_ORG_ID)`). Scoping to the platform org keeps tenant developers from receiving platform internals.
5. Send through `get_mail_sender()` with a 60 s timeout. Record the result (`SENT` / `FAILED` / `NO_RECIPIENTS`, recipient count, error).
6. Admins and Developers can **Preview** the report and **Send a test to myself**.

### 2.8 Permissions (enforced in the backend; the UI mirrors it)

`SA` = `SUPER_ADMIN`, `DEV` = `DEVELOPER`. **Every other role gets 403; anonymous gets 401.**

| Capability | SA | DEV |
|---|---|---|
| Document import (upload, list, retry, republish, queues, unpublish, preview) | ✅ | ❌ |
| Integrations: create / edit / delete source, credentials, schedule, Sync now, retry | ✅ | ❌ |
| Integrations: view sources (IDs shown, **secret never**), runs, files; **connection diagnostics** | ✅ | ✅ |
| CRM entities (business data, may contain PII) | ✅ | ❌ |
| Monitoring: health, run checks, events, failures, activity log, report status and preview, test mail to self | ✅ | ✅ |

**Backend guards:**
- `require_roles(ROLE_SUPER_ADMIN)` and `require_roles(ROLE_DEVELOPER)` (SA passes the latter automatically).
- **Never** `require_roles_or_nav_permission`.
- An automated test enumerates every doc-intel route and asserts the matrix, so no route can ship unguarded.

**UI:**
- New nav items carry `lockedRoles`. Locked items ignore page-key permissions entirely.
- They are excluded from the Roles defaults and the per-user page-access editor, so they cannot be granted to anyone.
- `/document-import` and `/integrations` are SA only; `/monitoring` is SA + DEV.
- No backend nav-catalog change is needed.

### 2.9 Security design

**Credentials at rest**
- Tenant ID, Client ID and Client Secret are each encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256, `cryptography`) and stored as `fernet:v1:<token>`.
- Keys come from `DOC_INTEL_SECRETS_KEY`: a comma-separated **MultiFernet** list; the first key encrypts and all keys decrypt, which allows rotation.
- The key is generated with `python -m backend.doc_intel.crypto generate-key`. It never goes in the DB, the repo or `.env.example`.
- **If the key is missing:** credential writes and tests return 503 ("encryption key not configured"), monitoring shows an actionable red status, and the rest of AIVA is unaffected.

**Secret handling**
- The secret is **write-only**. Responses expose only `client_secret_set`, `secret_updated_at` and a 4-character hint.
- The request model uses `SecretStr`; values are never logged, never put in query strings (HTTP logs store query strings) and never passed into exception text (the 500 handler echoes exceptions).
- MSAL errors are scrubbed.
- Audit rows record "secret rotated", never values.
- UI: never prefilled, `autocomplete="new-password"`, cleared after save, not kept in the query cache.

**Upload hardening**
- SA-only; extension and magic bytes; size and count caps; sanitized filenames; random storage directories outside any web root; files are never served back.
- Parsing happens in a child process with the library's zip-bomb, pixel and page limits and a hard timeout. DOCX is parsed as XML (no macros).

**Data egress**
- Flow 1 extraction is fully local; only embeddings go to the existing provider.
- Flow 2 sends text to the LLM only when the source's toggle is on (D3).
- The repo's `document_extractor.toml` is never loaded, because explicit options bypass it.
- The LLM key is passed to the child process explicitly. The library would otherwise send text with a placeholder key.

**SSRF**
- The Graph base URL and authority are fixed server-side (overridable only by env for sovereign clouds).
- The site URL host must match `*.sharepoint.com`.

**Least privilege:** read-only Graph application permissions. `Sites.Selected` with a per-site grant is recommended.

**Licensing:** the `pdfium` engine and pypdfium2 rendering are used, and PyMuPDF (AGPL) is not installed in the image.

**Public repositories:** only placeholders in `.env.example`. This document contains no hosts, keys or credentials.

### 2.10 Work breakdown (multi-agent)

**Contracts first:** the lead writes `doc_intel/schemas.py`, `doc_intel/schema.py` (DDL) and the TypeScript types, so the agents build against fixed interfaces. File ownership is disjoint so agents can work in parallel.

| Agent | Owns | Deliverables |
|---|---|---|
| **1 — Architecture analysis** | — (read-only) | Done: §1 of this document |
| **2 — Backend** | `doc_intel/{kb_import,kb_publish,chunking,health,scheduler,weekly_report,runtime}.py`, `routers/{kb_documents,monitoring}.py`, `main.py` and `routers/__init__.py` registration | Flow 1 end to end, monitoring, scheduler, weekly report |
| **3 — Frontend** | `AIVA-V2-UI/src/**` (new pages, components and hooks; the 4 existing-file edits) | 3 pages, role-locked nav, polling, dark mode |
| **4 — Integrations & security** | `doc_intel/{crypto,storage,extraction,graph_source,crm_store,crm_sync,guards,settings}.py`, `routers/integrations.py`, `crm-document-ingestion` listing methods, requirements, Dockerfile, `.env.example` | Encryption, uploads, extraction runner, Graph listing and sync, CRM store, packaging |
| **5 — Review & testing** | `backend/tests/**`, review report | Existing and new tests, security review, permission review, findings fixed or reported |

### 2.11 Change register

Risk levels: **Low**, **Medium**, **High**. Everything is additive unless stated otherwise.

| ID | Change | Reason | Affected files | Risk | Migration impact |
|---|---|---|---|---|---|
| N1 | New backend package `doc_intel` | The isolated home for all new logic | `backend/doc_intel/**` (new) | Medium: new code in the API process. Mitigated by the child process, flags, try/except and tests | Creates 9 tables at first start (§3) |
| N2 | New backend test suite | The backend has no tests today | `backend/tests/**` (new), `backend/requirements-dev.txt` (new) | Low | None |
| N3 | Graph listing in `crm-document-ingestion`: paged children, recursion, site/drive/path resolution, `folder`/`deleted` facets, cTag | The sync needs listing; the library only fetches single items | `crm-document-ingestion/src/crm_ingestion/connectors/sharepoint/{client,models,downloader}.py`, tests | Low: additive methods; the library has no other users; 123 existing tests must stay green | None |
| N4 | Admin CLI: `unpublish-all`, `drop-tables`, `generate-key` | Rollback and key setup | `backend/doc_intel/admin.py` (new) | Low | Used only for rollback |
| N5 | New UI pages, components and hooks | Required UI | `AIVA-V2-UI/src/pages/{DocumentImportPage,IntegrationsPage,MonitoringPage}.tsx`, `src/components/doc-intel/**`, `src/hooks/{useDocumentImport,useIntegrations,useMonitoring}.ts` (all new) | Low | Deploy the UI |
| **E1 EXISTING** | Include 3 routers | Register the endpoints | `backend/routers/__init__.py` (+4 lines) | Low | None |
| **E2 EXISTING** | Lifespan hooks: a **read-only** check that the doc-intel tables exist (the module reports "not installed — run migration V001" and disables itself when they don't), start/stop the doc-intel worker, and (Phase 2) start/stop the scheduler when enabled. **No DDL at startup** (D5). | Background processing; controlled, reviewed migrations | `backend/main.py` (+~15 lines) | Low–Medium: startup path. Mitigated because it never raises and runs no DDL | None at boot. Tables come from migration V001 (§3.1) |
| **E3 EXISTING** | Dependencies: `cryptography`, `msal` | Encryption and Graph auth | `backend/requirements.txt` (+2 lines) | Low | `pip install` on deploy |
| **E4 EXISTING** | Docker image: `tesseract-ocr`, `tesseract-ocr-ara`, `-eng`; pinned `document-extractor` wheel installed with `--no-index` from `wheels/`; `crm-document-ingestion` installed; build arg `WITH_DOC_INTEL=1` | Extraction and OCR in production | `AIVA-V2/Dockerfile`, `.dockerignore` | Medium: image +~200 MB; build needs the wheel in `wheels/`. Without the build arg the image is unchanged | Rebuild the image; copy the wheel first |
| **E5 EXISTING** | Compose: bind-mount `./data/doc_intel`; `DOC_INTEL_STORAGE_DIR` | Uploads must survive rebuilds | `D:\AIVA\docker-compose.yml` (**not under git**; the server copy must be edited by hand) | Low | Create the directory and include it in backups |
| **E6 EXISTING** | Document the new env vars (placeholders only) | Operability | `backend/.env.example` | None | None |
| **E7 EXISTING (data)** | Publishing edits `kb_corpus.config_json.queue_groups[*].verticals` (appends `kbdoc-<id>`); materializes the default groups when a corpus has none | Queue availability without changing retrieval | Runtime data only (code in `doc_intel/kb_publish.py`) | Medium: live retrieval config. Mitigated by the row lock, raw read-modify-write that preserves every key, unit tests, integrity check and unpublish | Per document; reversible by unpublish |
| **E8 EXISTING (data)** | New `kb_chunk` rows (`external_parent_id = kbdoc-<id>`, `chunker_version = docimport-1`) | The knowledge itself | Runtime data only | Low | Reversible by unpublish |
| **E9 EXISTING — optional** | `build_kb_sources` skips chunks whose `payload.source == "document_import_v1"` (3 lines) | Otherwise agents see a broken `…view.php?id=kbdoc-12` link when an imported document ranks first. Existing chunks never carry that marker, so existing links are unchanged | `backend/services/rag.py` | Low | None. **If rejected:** imported documents may show a broken source link |
| **E10 EXISTING** | Role-locked nav: `lockedRoles` on `NavItem`, 3 items, a role check in `canAccessPermission` for locked items, locked items excluded from configurable lists | Pages visible only to SA/DEV and never grantable | `AIVA-V2-UI/src/lib/roles.ts` | Medium: access-control code. Existing items have no `lockedRoles`, so their logic is unchanged; covered by the manual role matrix | None |
| **E11 EXISTING** | Routes | New pages | `AIVA-V2-UI/src/App.tsx` (+3 imports, +3 entries) | Low | None |
| **E12 EXISTING** | Icons | Sidebar | `AIVA-V2-UI/src/components/shared/Layout.tsx` (+2 icons) | Low | None |
| **E13 EXISTING** | Types appended | Typed API | `AIVA-V2-UI/src/types/api.ts` (append only) | None | None |

**Explicitly not changed:**
- `embedding_service/**`, all existing routers and services, `role_nav_permissions.py`, `notifications.py`, `email/templates.py`, `system_health.py`;
- all existing tables and endpoints;
- `IngestionPage`, `LogsPage`, `ProtectedRoute`, `AuthContext`, `api-client.ts`, `index.css`.

**Optional follow-up (not in this scope):** make `scripts/seed_queue_groups.py` preserve `kbdoc-*` verticals. Today it would unpublish imported documents; the integrity check detects this.

**Effort estimate:** foundations 1 d · Flow 1 (backend + UI) 2–3 d · monitoring, scheduler and e-mail 2 d · Flow 2 (connector, sync, CRM store, UI) 3–4 d · review and hardening 1–2 d. **Total ≈ 9–12 developer-days.**

---

## 3. Database changes

### 3.1 Migration scripts (D5)

- **Location:** `backend/doc_intel/migrations/`. Each version has a forward script `V00N__<name>.sql` and a rollback script `V00N__<name>.rollback.sql`.
- **Runner:** `python -m backend.doc_intel.migrate <verify|show|apply|rollback> --version 00N`.
  - **`verify`** is read-only. It checks:
    - session user, current schema, container and service;
    - `CREATE TABLE` privilege and tablespace quota;
    - name collisions for every object in the script;
    - that tables the module only *reads* (`kb_corpus`, `kb_chunk`, `AIVA_accounts`, `AIVA_users`) are owned by the connected schema.
  - **`show`** prints the SQL.
  - **`apply` and `rollback`** require `--confirm-schema <SCHEMA>` (it must equal the connected schema) and `--yes`. They record the version in `AIVA_di_schema_version`.
- **Rollback safety:** a rollback refuses to drop `AIVA_kb_documents` while any document is `PUBLISHED`. Unpublish first.
- **V001 (Phase 1):** `AIVA_kb_documents`, `AIVA_health_checks`, `AIVA_health_check_events` and `AIVA_di_schema_version`, with their indexes and constraints.
- **V002 (Phase 2):** `AIVA_crm_sources`, `AIVA_crm_source_files`, `AIVA_crm_sync_runs`, `AIVA_crm_entities`, `AIVA_scheduler_jobs` and `AIVA_scheduler_runs`.
- **Test fixtures:** `backend/tests/doc_intel/sql/DI_TEST_kb_tables{,.rollback}.sql` create and drop `DI_TEST_KB_CORPUS` and `DI_TEST_KB_CHUNK`. These are isolated copies of the KB tables, used only by integration tests.

**Execution log**

| When (UTC) | Action | Result |
|---|---|---|
| 2026-09-24 11:24 | `verify --version 001` (read-only) | Schema `AI_ASSISTANT` in `FREEPDB1` (Oracle 23.26.2). `CREATE TABLE` granted, `USERS` quota unlimited. **No name collisions.** All read-only tables (`KB_CORPUS`, `KB_CHUNK`, `AIVA_ACCOUNTS`, `AIVA_ORGANIZATIONS`, `AIVA_USERS`, `AIVA_ROLES`, `AIVA_USER_ROLES`) are owned by `AI_ASSISTANT`. **Live traffic:** `AIVA_HTTP_REQUEST_LOGS` had a row from 5 min earlier. |
| 2026-09-24 11:44 | `apply --version 001` (approved by the user after reviewing the SQL and objects) | 8/8 statements OK. Ledger row `001` written. All objects `VALID`. |
| 2026-09-24 11:44 | `apply --file …/DI_TEST_kb_tables.sql` (approved) | 4/4 statements OK. These must be dropped after the integration tests. |
| 2026-09-25 | Integration tests and end-to-end runs | Only the 3 V001 data tables and the DI_TEST copies were written. All test rows were deleted afterwards: 0 rows remain in each. |
| 2026-09-25 | `rollback --file …/DI_TEST_kb_tables.sql` (approved as part of the plan) | 2/2 `DROP … PURGE` OK. The DI_TEST tables are gone, with no recycle-bin entries. **V001 tables remain, empty, for deployment.** |
| 2026-09-25 18:36 | `verify --version 002` (read-only) → `apply --version 002` (approved by the user after reviewing the SQL and objects) | 10/10 statements OK. Created 4 tables (`AIVA_CRM_SOURCES`, `AIVA_CRM_SYNC_RUNS`, `AIVA_CRM_SOURCE_FILES`, `AIVA_CRM_ENTITIES`), 17 indexes (including the unique active-run index), 6 LOB segments and 4 identity sequences, all `VALID`. Ledger row `002` written. |

### 3.2 Table definitions

- **Ten new tables in total**, created only through the migration scripts above: the nine below plus `AIVA_di_schema_version`.
- **No ALTER** of any existing table and **no foreign keys to existing tables.** Existing account, org and user deletion flows work from explicit table lists, and an FK would interfere with them. Orphans are tolerated and shown as "account deleted".
- **All doc-intel timestamps are UTC** (`SYS_EXTRACT_UTC(SYSTIMESTAMP)`) and are returned as ISO-8601 with `Z`.
- **Text columns that may hold Arabic** use `CHAR` length semantics. Errors are truncated by bytes before writing.

```sql
-- Flow 1
CREATE TABLE AIVA_kb_documents (
  id                 NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  batch_id           VARCHAR2(32),
  account_id         NUMBER NOT NULL,
  corpus_id          VARCHAR2(32) NOT NULL,
  queue_keys         CLOB,                         -- JSON array of queue keys
  vertical           VARCHAR2(64),                 -- kbdoc-<id>
  filename           VARCHAR2(255 CHAR) NOT NULL,
  content_type       VARCHAR2(128),
  size_bytes         NUMBER,
  sha256             VARCHAR2(64),
  storage_dir        VARCHAR2(1024),
  status             VARCHAR2(16) DEFAULT 'QUEUED' NOT NULL,   -- QUEUED|PROCESSING|PUBLISHED|FAILED|UNPUBLISHED
  upload_status      VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,  -- PENDING|RUNNING|COMPLETED|FAILED|SKIPPED
  extraction_status  VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  chunking_status    VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  embedding_status   VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  publishing_status  VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  failed_stage       VARCHAR2(16),
  error_message      VARCHAR2(2000 CHAR),
  stage_details      CLOB,                         -- JSON: per-stage started/finished/metrics
  warnings_json      CLOB,
  page_count         NUMBER,
  chunk_count        NUMBER,
  tokens_used        NUMBER,
  cost_usd           NUMBER,
  attempts           NUMBER DEFAULT 0 NOT NULL,
  worker_id          VARCHAR2(128),
  uploaded_by        NUMBER,
  created_at         TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  updated_at         TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  started_at         TIMESTAMP(6),
  finished_at        TIMESTAMP(6),
  published_at       TIMESTAMP(6)
);
CREATE INDEX idx_aiva_kbdoc_account ON AIVA_kb_documents (account_id, created_at);
CREATE INDEX idx_aiva_kbdoc_status  ON AIVA_kb_documents (status, created_at);

-- Flow 2
CREATE TABLE AIVA_crm_sources (
  id                  NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  name                VARCHAR2(128 CHAR) NOT NULL,
  account_id          NUMBER,
  provider            VARCHAR2(32) DEFAULT 'microsoft_graph' NOT NULL,
  tenant_id_enc       VARCHAR2(1024),
  client_id_enc       VARCHAR2(1024),
  client_secret_enc   VARCHAR2(2048),
  client_secret_hint  VARCHAR2(8),
  secret_updated_at   TIMESTAMP(6),
  site_url            VARCHAR2(1024),
  drive_name          VARCHAR2(255 CHAR),
  folder_path         VARCHAR2(1024 CHAR),
  recursive           NUMBER(1) DEFAULT 1 NOT NULL,
  file_extensions     VARCHAR2(256) DEFAULT '.pdf,.docx' NOT NULL,
  use_intelligence    NUMBER(1) DEFAULT 0 NOT NULL,
  resolved_site_id    VARCHAR2(256),
  resolved_drive_id   VARCHAR2(256),
  resolved_folder_id  VARCHAR2(256),
  sync_enabled        NUMBER(1) DEFAULT 0 NOT NULL,
  sync_interval_days  NUMBER DEFAULT 14 NOT NULL,
  sync_hour           NUMBER DEFAULT 2 NOT NULL,
  next_sync_at        TIMESTAMP(6),
  last_sync_at        TIMESTAMP(6),
  last_sync_status    VARCHAR2(16),
  last_sync_error     VARCHAR2(2000 CHAR),
  last_success_at     TIMESTAMP(6),
  status              VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL,   -- ACTIVE|DISABLED|DELETED
  created_by          NUMBER,
  updated_by          NUMBER,
  created_at          TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  updated_at          TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL
);

CREATE TABLE AIVA_crm_source_files (
  id                   NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  source_id            NUMBER NOT NULL,
  drive_id             VARCHAR2(256) NOT NULL,
  item_id              VARCHAR2(256) NOT NULL,
  name                 VARCHAR2(512 CHAR),
  path                 VARCHAR2(2000 CHAR),
  web_url              VARCHAR2(2000),
  etag                 VARCHAR2(512),
  ctag                 VARCHAR2(512),
  quick_xor_hash       VARCHAR2(128),
  content_sha256       VARCHAR2(64),
  size_bytes           NUMBER,
  modified_at          TIMESTAMP(6),
  state                VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL,     -- ACTIVE|DELETED
  status               VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,    -- PENDING|PROCESSING|COMPLETED|FAILED|SKIPPED
  download_status      VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  extraction_status    VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  intelligence_status  VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  entities_status      VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  persist_status       VARCHAR2(16) DEFAULT 'PENDING' NOT NULL,
  failed_stage         VARCHAR2(16),
  error_message        VARCHAR2(2000 CHAR),
  warnings_json        CLOB,
  result_json          CLOB,                                       -- CRMProcessingResult JSON
  entity_count         NUMBER,
  is_valid             NUMBER(1),
  attempts             NUMBER DEFAULT 0 NOT NULL,
  last_run_id          NUMBER,
  first_seen_at        TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  last_seen_at         TIMESTAMP(6),
  processed_at         TIMESTAMP(6),
  deleted_at           TIMESTAMP(6),
  updated_at           TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  CONSTRAINT uq_aiva_crm_file_item UNIQUE (source_id, drive_id, item_id)
);
CREATE INDEX idx_aiva_crm_files_status ON AIVA_crm_source_files (source_id, status);

CREATE TABLE AIVA_crm_sync_runs (
  id               NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  source_id        NUMBER NOT NULL,
  trigger_type     VARCHAR2(16) NOT NULL,                 -- SCHEDULED|MANUAL
  triggered_by     NUMBER,
  status           VARCHAR2(16) DEFAULT 'RUNNING' NOT NULL, -- RUNNING|COMPLETED|PARTIAL|FAILED
  files_seen       NUMBER DEFAULT 0 NOT NULL,
  files_new        NUMBER DEFAULT 0 NOT NULL,
  files_changed    NUMBER DEFAULT 0 NOT NULL,
  files_deleted    NUMBER DEFAULT 0 NOT NULL,
  files_unchanged  NUMBER DEFAULT 0 NOT NULL,
  files_failed     NUMBER DEFAULT 0 NOT NULL,
  error_message    VARCHAR2(2000 CHAR),
  details_json     CLOB,
  started_at       TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  finished_at      TIMESTAMP(6)
);
CREATE INDEX idx_aiva_crm_runs_source ON AIVA_crm_sync_runs (source_id, started_at);

CREATE TABLE AIVA_crm_entities (
  id              NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  source_id       NUMBER NOT NULL,
  source_file_id  NUMBER NOT NULL,
  account_id      NUMBER,
  entity_type     VARCHAR2(64) NOT NULL,
  display_value   VARCHAR2(512 CHAR),
  match_key       VARCHAR2(512 CHAR),
  confidence      NUMBER,
  is_valid        NUMBER(1),
  fields_json     CLOB,                                   -- to_crm_json() entity incl. _meta provenance
  issues_json     CLOB,
  status          VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL,  -- ACTIVE|WITHDRAWN
  created_at      TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  updated_at      TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  withdrawn_at    TIMESTAMP(6)
);
CREATE INDEX idx_aiva_crm_ent_file ON AIVA_crm_entities (source_file_id, status);
CREATE INDEX idx_aiva_crm_ent_type ON AIVA_crm_entities (entity_type, status);

-- Monitoring + scheduling
CREATE TABLE AIVA_health_checks (
  component_key         VARCHAR2(64) PRIMARY KEY,
  label                 VARCHAR2(128),
  status                VARCHAR2(16) NOT NULL,           -- HEALTHY|FAILED|NOT_CONFIGURED
  reason                VARCHAR2(2000 CHAR),
  suggested_action      VARCHAR2(1000 CHAR),
  details_json          CLOB,
  latency_ms            NUMBER,
  checked_at            TIMESTAMP(6) NOT NULL,
  last_success_at       TIMESTAMP(6),
  last_failure_at       TIMESTAMP(6),
  consecutive_failures  NUMBER DEFAULT 0 NOT NULL
);

CREATE TABLE AIVA_health_check_events (
  id             NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  component_key  VARCHAR2(64) NOT NULL,
  old_status     VARCHAR2(16),
  new_status     VARCHAR2(16) NOT NULL,
  reason         VARCHAR2(2000 CHAR),
  created_at     TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL
);
CREATE INDEX idx_aiva_health_evt_time ON AIVA_health_check_events (created_at);

CREATE TABLE AIVA_scheduler_jobs (
  job_key           VARCHAR2(128) PRIMARY KEY,
  lease_owner       VARCHAR2(128),
  lease_until       TIMESTAMP(6),
  last_started_at   TIMESTAMP(6),
  last_finished_at  TIMESTAMP(6),
  last_status       VARCHAR2(16),
  last_error        VARCHAR2(2000 CHAR),
  updated_at        TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL
);

CREATE TABLE AIVA_scheduler_runs (
  id             NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  job_key        VARCHAR2(128) NOT NULL,
  period_key     VARCHAR2(64) NOT NULL,                  -- e.g. weekly_report / 2026-09-27
  status         VARCHAR2(16) NOT NULL,                  -- RUNNING|SENT|FAILED|NO_RECIPIENTS
  details_json   CLOB,
  error_message  VARCHAR2(2000 CHAR),
  started_at     TIMESTAMP(6) DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  finished_at    TIMESTAMP(6),
  CONSTRAINT uq_aiva_sched_run UNIQUE (job_key, period_key)
);
```

**Writes to existing structures (data only, no DDL):**
- `kb_corpus.config_json.queue_groups[*].verticals` gets `kbdoc-<id>` added or removed.
- `kb_chunk` gets rows with `external_parent_id LIKE 'kbdoc-%'` inserted or deleted.
- Both are reversible per document; see E7 and E8.

**Storage:** the filesystem holds `DOC_INTEL_STORAGE_DIR/kb/<doc-id>-<random>/{original.<ext>, normalized.json}`. CRM downloads are processed in memory or temp files and not kept.

**Volume estimate:** tens to hundreds of rows per month, plus about 100 health events per month. This is negligible next to the existing log tables. Retention jobs cap growth (§2.6).

---

## 4. API changes

**No existing endpoint changes.**

- All new routes live under `/api/doc-intel` and return `{"detail": "..."}` errors with explicit messages. A 403 says *why* ("Restricted to Super Admins"), so the UI does not show "session expired".
- Lists use the existing `{items, limit, offset}` shape.
- Timestamps are UTC ISO-8601.

| Method & path | Roles | Request | Response |
|---|---|---|---|
| `POST /doc-intel/kb-documents` | SA | multipart: `account_id`, `queue_keys` (repeated, ≥1), `files` (repeated, 1–20) | **202** `{batch_id, documents: KbDocumentOut[]}`. Rejected files come back with upload `FAILED` + reason |
| `GET /doc-intel/kb-documents` | SA | `account_id?`, `status?`, `batch_id?`, `limit`, `offset` | `{items: KbDocumentOut[], limit, offset}` |
| `GET /doc-intel/kb-documents/{id}` | SA | — | `KbDocumentOut` (stages, warnings, metrics) |
| `GET /doc-intel/kb-documents/{id}/preview` | SA | `max_chars?` | `{pages: [{number, text}]}` (extracted text, truncated) |
| `POST /doc-intel/kb-documents/{id}/retry` | SA | — | 202 `KbDocumentOut` (resumes from the failed stage) |
| `POST /doc-intel/kb-documents/{id}/republish` | SA | — | 202 `KbDocumentOut` |
| `PATCH /doc-intel/kb-documents/{id}/queues` | SA | `{queue_keys: string[]}` | `KbDocumentOut` (config-only) |
| `DELETE /doc-intel/kb-documents/{id}` | SA | — | `KbDocumentOut` (`UNPUBLISHED`) |
| `GET /doc-intel/sources` | SA, DEV | — | `SourceOut[]` (secret never included) |
| `POST /doc-intel/sources` | SA | `SourceCreate` | 201 `SourceOut` |
| `GET /doc-intel/sources/{id}` | SA, DEV | — | `SourceOut` |
| `PATCH /doc-intel/sources/{id}` | SA | `SourceUpdate` (omit `client_secret` to keep it) | `SourceOut` |
| `DELETE /doc-intel/sources/{id}` | SA | — | 204 (soft delete; entities kept as withdrawn) |
| `POST /doc-intel/sources/{id}/test` | SA, DEV | — | `ConnectionTestOut {ok, steps[{key,label,ok,latency_ms,detail,suggested_action}], checked_at}` |
| `POST /doc-intel/sources/{id}/sync` | SA | — | 202 `SyncRunOut` |
| `GET /doc-intel/sources/{id}/runs` | SA, DEV | `limit`, `offset` | `{items: SyncRunOut[], …}` |
| `GET /doc-intel/sources/{id}/files` | SA, DEV | `status?`, `state?`, `limit`, `offset` | `{items: SourceFileOut[], …}` (per-file stages) |
| `POST /doc-intel/source-files/{id}/retry` | SA | — | 202 `SourceFileOut` |
| `GET /doc-intel/crm/entities` | SA | `source_id?`, `entity_type?`, `status?`, `q?`, `limit`, `offset` | `{items: CrmEntityOut[], …}` |
| `GET /doc-intel/crm/entities/{id}` | SA | — | `CrmEntityOut` with fields and provenance |
| `GET /doc-intel/monitoring/health` | SA, DEV | — | `{overall, checked_at, stale, throttled, components: HealthComponentOut[]}` (a `scheduler` block is added in Phase 2) |
| `POST /doc-intel/monitoring/health/run` | SA, DEV | `component?` | same as above, freshly checked (once per 30 s) |
| `GET /doc-intel/monitoring/events` | SA, DEV | `limit` | `HealthEventOut[]` |
| `GET /doc-intel/monitoring/failures` | SA, DEV | `days` (default 7) | `{days, items: [{kind, id, title, account_name, stage, reason, occurred_at}]}`. `kind` is `kb_document` in Phase 1; `crm_file` and `sync_run` arrive in Phase 2 |
| `GET /doc-intel/monitoring/activity` | SA, DEV | `limit` | Unified activity feed (the "logs" view) |
| `GET /doc-intel/monitoring/report` | SA, DEV | — | `{enabled, schedule, next_run_at, last_run, recipients_count}` |
| `POST /doc-intel/monitoring/report/preview` | SA, DEV | — | `{subject, html, text}` |
| `POST /doc-intel/monitoring/report/send-test` | SA, DEV | — | `{status, recipients: [self], detail}` |

**Key models:**
- `SourceOut` includes `tenant_id` and `client_id`, visible to SA/DEV as the requirement allows, plus `client_secret_set`, `client_secret_hint` and `secret_updated_at`, **never** the secret. `SourceCreate` and `SourceUpdate` use `SecretStr` for `client_secret`.
- `HealthComponentOut` = `{key, label, status: HEALTHY|FAILED|NOT_CONFIGURED, reason, suggested_action, checked_at, last_success_at, last_failure_at, consecutive_failures, latency_ms, details}`.
- `KbDocumentOut.stages` = `[{name: upload|extraction|chunking|embedding|publishing, status, error, started_at, finished_at}]`.
- `SourceFileOut.stages` = `[download|extraction|intelligence|entities|persist]`.

**Existing endpoints the UI reuses unchanged:** `GET /api/accounts` and `GET /api/accounts/{id}/kb-queues` (both pass for SA).

---

## 5. UI changes

**Navigation.** Three role-locked items are added after "Logs":

| Item | Path | Icon | Visible to |
|---|---|---|---|
| Document Import | `/document-import` | `FileUp` | SA |
| Integrations | `/integrations` | `Plug` | SA |
| Monitoring | `/monitoring` | `Activity` | SA + DEV |

- They never appear in Roles defaults or the per-user page-access editor.
- A direct URL visited by any other role redirects to `/`, which is the existing ProtectedRoute behaviour.
- **No agent, supervisor, account manager or org admin sees any of it.**

**Document Import page (SA).**
- **Controls:**
  1. Account select (only accounts with a knowledge base, labelled with the org name).
  2. Queue multi-select chips from `/accounts/{id}/kb-queues`, at least one required, reset when the account changes.
  3. Drop zone for PDF/DOCX, multiple files, client-side size and type pre-checks.
  4. Upload.
- **Documents table:** file, account, queues, and **five stage pills**:
  - grey: waiting;
  - blue spinner: running;
  - **green: completed**;
  - **red: failed**, showing the failure reason inline and in a tooltip.
  - Also shown: status, queue position, updated time.
- **Actions:** details (warnings, metrics, text preview), Retry, Republish, Change queues, Unpublish (with confirmation).
- Polls every 3 s while any document is still processing, and stops when all are finished.

**Integrations page (SA).**
- Sources list, and a create/edit dialog with:
  - name and optional account;
  - Tenant ID and Client ID;
  - **Client Secret**: a write-only field, never prefilled, "Stored · updated <date> · ends …abcd", with "leave blank to keep current";
  - site URL, library, folder, recursive, file types;
  - use-LLM toggle (D3);
  - schedule (1 / 2 / 3 / 4 weeks / custom days, hour, enabled) with next and last sync shown.
- Buttons: **Test connection** (step-by-step results) and **Sync now**.
- Tables:
  - recent runs (new / changed / deleted / failed, status);
  - tracked files with five stage pills (download / extraction / intelligence / entities / persist) and reasons;
  - CRM entities (type, value, confidence, valid, source file, provenance on click).

**Monitoring page (SA + DEV)**, with tabs:
- **Health:** six cards, each green, red or grey, showing reason, checked at, last successful check and **suggested action**; "Run checks now"; the weekly-report card (last sent, next run, recipients, Preview, Send test to me).
- **Failures:** documents, CRM files and sync runs from the last 7 days.
- **Logs:** the doc-intel activity feed, plus the existing error-log panel reused as-is.
- **Diagnostics:** per-source connection diagnostics, database and embedder latency, extractor versions and OCR languages.

**Conventions:**
- **Data:** existing hooks pattern (TanStack Query) and existing UI kit; the unused `apiUpload` helper for multipart.
- **Colors:** theme tokens and already-remapped emerald / red / amber shades only, so dark mode works with no `index.css` change.
- **Verification:** `tsc --noEmit` and a `vite build` into a temporary directory, so the tracked `dist/` is not rewritten.

---

## 6. Migration plan

**Pre-deploy (no production impact)**
1. Merge the code. Automation flags default to off.
2. Generate the key with `python -m backend.doc_intel.crypto generate-key`. Add `DOC_INTEL_SECRETS_KEY=…` to the **server** `AIVA-V2/.env` (never commit it) and store a copy in the team password manager. Without it, credentials cannot be decrypted.
3. System packages:
   - **Host style:** `sudo apt install tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng`.
   - **Docker style:** put `document_extractor-<pinned>.whl` in `AIVA-V2/wheels/` and build with `--build-arg WITH_DOC_INTEL=1`.
4. Python packages (host style), in the server venv:
   - `pip install -r backend/requirements.txt`
   - `pip install --no-index --find-links wheels "document-extractor==<pinned>"`
   - `pip install ./crm-document-ingestion`
5. Storage: create `data/doc_intel` (bind mount in compose, E5) and add it to the backup set.
6. DB: `python -m backend.doc_intel.migrate verify --version 001`, then `show`. Review the SQL and the affected objects, then run `apply --version 001 --confirm-schema <SCHEMA> --yes`. Phase 2 later applies V002 the same way.

**Deploy**
7. Restart the backend and check the log for `doc_intel: ready (schema V001)`. If the tables are missing, the module logs "not installed — run migration V001" and disables itself; the rest of AIVA runs normally.
8. Deploy the UI.

**Post-deploy verification**
9. As SA, open **Monitoring → Run checks now**. Database, Embedding and Extraction should be green; Microsoft and CRM grey ("not configured").
10. Import a small test PDF into a test queue. All five stages should turn green. In **Chat** with that queue, ask a question whose answer cites the document. Then **Unpublish** and confirm the answer no longer uses it.
11. Add the SharePoint source, run **Test connection**, then **Sync now**, and review files and entities.
12. **Role matrix:**
    - Developer: sees Monitoring only; the others are hidden and their API returns 403.
    - Org Admin and Agent: see nothing; direct URLs redirect and the API returns 403.
13. Enable automation: `DOC_INTEL_SCHEDULER_ENABLED=true`, then `DOC_INTEL_WEEKLY_REPORT_ENABLED=true`. Restart, then **Send test report**.

**Backward compatibility**
- Existing tables, endpoints, retrieval, pages and permissions are unchanged.
- The only live-data effect is per published document, and it is reversible.

**Rollback (least to most invasive)**

| Level | Action | Effect |
|---|---|---|
| 1 | Set both automation flags to `false` and restart | Stops scheduled syncs and e-mails |
| 2 | **Unpublish everything:** `python -m backend.doc_intel.admin unpublish-all --execute` (dry run without `--execute`) | Removes every `kbdoc-*` vertical and chunk. **Run before removing the code.** SQL equivalents are in the admin module's docstring |
| 3 | Redeploy the previous backend and UI | The new tables stay, inert |
| 4 | `python -m backend.doc_intel.migrate rollback --version 00N --confirm-schema <SCHEMA> --yes` (V002 first, then V001) | After a DB backup: drops only the doc-intel tables. Uploaded files can then be deleted from `data/doc_intel` |

---

## 7. Risk assessment

Likelihood and impact are rated L / M / H.

| # | Risk | L | I | Level | Mitigation |
|---|---|---|---|---|---|
| R1 | A local run writes to **production** (the local `.env` points to the production DB; startup DDL) | M | H | **High** | Never start the backend locally against it; use the disposable DB (D5); unit tests without a DB; explicit approval for any DB contact |
| R2 | OCR/extraction CPU and RAM load slows chat (single API process) | M | H | **High** | Child process, one document at a time, page and time caps, scheduled syncs at 02:00; future: move the worker to its own container |
| R3 | Poor Arabic extraction (reversed lam-alef, OCR noise) leads to wrong answers | H | M | **High** | Quality heuristic and OCR fallback, warnings surfaced, text preview, pilot on one queue, one-click unpublish |
| R4 | Retrieval config corruption from the `queue_groups` read-modify-write | L | H | Medium | Row lock; raw config preserved including unknown keys and Decimal handling; transform unit tests; `queue_groups` snapshot in `stage_details`; integrity check; unpublish |
| R5 | An existing REINDEX or the seed script wipes imported chunks or verticals | M | M | Medium | Integrity check with Republish; runbook warning; optional seed-script follow-up |
| R6 | Secret exposure (responses, logs, exception text, public repos) | L | H | Medium | Encryption; write-only `SecretStr`; scrubbing; tests asserting secrets never appear in responses or logs; placeholders only in examples |
| R7 | Losing the encryption key makes stored credentials unrecoverable | L | M | Low | Key backup procedure, MultiFernet rotation, re-enter credentials |
| R8 | Permission leak (page-key bypass, per-user extras) | M | H | **High** | Role-only guards, locked nav, automated route × role matrix test, manual check per role |
| R9 | Document text sent to an LLM unintentionally | M | M | Medium | Explicit extractor options; per-source toggle (D3); TOML never loaded; Flow 1 intelligence off |
| R10 | Graph problems: throttling, very large libraries, expired secret | M | M | Medium | Paging, backoff (existing 429/503 retry), caps, AADSTS-specific suggested actions, secret-age warning after 330 days |
| R11 | A partial listing marks files as deleted | L | H | Medium | Deletions only after a complete listing; `PARTIAL` status; soft withdrawal is recoverable |
| R12 | Scheduler double runs (multiple processes or replicas) | L | M | Low | DB leases and unique period keys |
| R13 | New schema code blocks startup | L | H | Medium | try/except isolation; the module disables itself; idempotent DDL tested twice on the disposable DB |
| R14 | Packaging: proprietary wheel, Python 3.12 (image) vs 3.13 (local), supply chain | M | M | Medium | Pinned version with `--no-index`, build arg, a test run on both versions |
| R15 | AGPL PyMuPDF obligations | M | M | Medium | pdfium engine and pypdfium2 rendering; the `mupdf` extra is not installed |
| R16 | Upload volume fills the disk (a disk-full outage happened on 2026-09-12) | M | M | Medium | Size and count caps, retention setting, the existing watchdog disk alert |
| R17 | KB pool contention (max 8, shared with chat) | L | M | Low | Embeddings hold no connection; one short publish transaction |
| R18 | Broken source links for imported documents | H | L | Medium | E9, or accept |
| R19 | Malicious uploads | L | H | Medium | SA-only, validation, child-process parsing with library limits, files never served |
| R20 | Unclear deployment style (Docker vs tmux) | M | M | Medium | The runbook covers both; confirm (D6) |
| R21 | Queues belong to a corpus, so documents are visible to every account sharing it; unrestricted sessions see all | M | L | Low | Documented; the UI warns when the corpus is shared |
| R22 | The weekly report reaches tenant developers | L | M | Low | Scoped to `NOTIFY_PLATFORM_ORG_ID` |
| R23 | Timezone mistakes (Sunday 09:00 Cairo, UTC storage) | L | L | Low | `ZoneInfo` and unit tests |
| R24 | *(Existing, not caused by this work)* Client material (`6 Digits.pdf` and its extraction) is committed to `AIVA-V2`, which was public as of August | — | H | — | Flagged for the owners: consider removing it from git history or making the repo private |

---

## 8. Test & verification plan

**Existing suites (must stay green)**
- `crm-document-ingestion`: 123 offline tests, run in its own `.venv`.
- `llm_service/tests/unit`.
- UI: `tsc --noEmit` and a `vite build` into a temporary directory.
- `haiva-blackbox-tester` needs a live server and DB, so it runs only against a disposable environment, never production.

**New unit / API tests** (`backend/tests`, pytest, fake DB, no network)

| Area | What is tested |
|---|---|
| Crypto | Round trip, rotation, tampered token, missing key fails closed |
| Permissions | Every doc-intel route × {SA, DEV, ORG_ADMIN, ACCOUNT_MANAGER, SUPERVISOR, AGENT, anonymous} → expected 2xx / 403 / 401; the test fails if a new route has no guard |
| Secrets | Create/update then GET never contains the secret; captured logs and audit values never contain it |
| Upload validation | Type, magic bytes, size, count, filename sanitization, duplicates |
| Flow 1 state machine | Success path; failure at each stage (red plus reason, later stages pending); retry; restart recovery |
| `queue_groups` transform | Add, remove, change queues, materialize defaults, preserve unknown keys, idempotency |
| Chunk builder | Header, size limits, pages, empty input, parent-id length ≤ 64 |
| Sync diff | New / changed / deleted / unchanged; a partial listing deletes nothing; retries |
| Graph listing (in `crm-document-ingestion`, `MockTransport`) | Paging, recursion, facets, 429 |
| Health | Error → reason / suggested action mapping (AADSTS, ORA, 401); status transitions produce events |
| Scheduler | `next_sync_at` (interval + hour, Cairo); lease acquisition; weekly period key and catch-up; idempotency |
| Weekly report | Healthy and failing compositions; recipient scoping; send failure is recorded |

**Integration** (the configured Oracle schema, per D5)
- Run migration V001 `verify`, `apply`, `verify`, then `rollback`, then `apply` again. This proves both scripts and the ownership checks.
- Publish, unpublish and change queues against the isolated `DI_TEST_KB_CORPUS` / `DI_TEST_KB_CHUNK` copies. This covers the VECTOR insert, the `SELECT … FOR UPDATE` config edit and the delete-by-parent.
- A vector-distance query with the resolved verticals returns the document's chunks only for queues that were selected.
- **No statement touches an existing table** except read-only `SELECT`s (the health checks and the integrity check).

**Security review** (Agent 5)
- Authz, secret handling, input validation, SSRF, logging hygiene, dependency review and data egress.
- Findings are fixed or reported with their severity.

**Permission review**
- The role matrix above, checked against the code, the automated tests and the UI.
- Includes direct-URL access for every role.

**Manual E2E**, in staging or on the disposable environment: upload → chat answer cites the document → unpublish → the answer no longer uses it; SharePoint test connection and sync against a test site.

---

## Appendix A — New environment variables (defaults are safe)

| Variable | Default | Purpose |
|---|---|---|
| `DOC_INTEL_ENABLED` | `true` | Mount the doc-intel routers and run the import worker |
| `DOC_INTEL_SECRETS_KEY` | *(unset)* | Fernet key(s), comma-separated for rotation. Required for integrations |
| `DOC_INTEL_STORAGE_DIR` | `data/doc_intel` | Uploaded originals and normalized JSON |
| `DOC_INTEL_MAX_UPLOAD_MB` / `DOC_INTEL_MAX_FILES_PER_UPLOAD` | `50` / `20` | Upload limits |
| `DOC_INTEL_MAX_PAGES` / `DOC_INTEL_MAX_CHUNKS` | `300` / `2000` | Extraction and cost limits |
| `DOC_INTEL_EXTRACTION_TIMEOUT_SECONDS` | `900` | Hard timeout per document |
| `DOC_INTEL_OCR_LANGUAGES` | `ara,eng` | Tesseract languages |
| `DOC_INTEL_TESSERACT_CMD` | *(unset)* | Tesseract path when it is not on PATH |
| `DOC_INTEL_SCHEDULER_ENABLED` | `false` | Health checks, scheduled syncs and retention |
| `DOC_INTEL_HEALTH_INTERVAL_MINUTES` | `15` | Health check cadence |
| `DOC_INTEL_WEEKLY_REPORT_ENABLED` / `DOC_INTEL_WEEKLY_REPORT_HOUR` | `false` / `9` | Sunday report |
| `DOC_INTEL_TIMEZONE` | `Africa/Cairo` | Schedules |
| `DOC_INTEL_SYNC_MAX_FILES` | `5000` | Listing cap per sync |
| `DOC_INTEL_CRM_SCHEMA_PATHS` | `[]` | Client CRM schema JSON files |
| `DOC_INTEL_ORIGINALS_RETENTION_DAYS` | `0` | 0 = keep uploaded originals |

## Appendix B — Status colors

| Status | Stage pill | Health card |
|---|---|---|
| `COMPLETED` / `HEALTHY` | green (emerald) | green |
| `FAILED` | red, with the reason shown | red, with the reason and suggested action |
| `RUNNING` | blue spinner | — |
| `PENDING` / `SKIPPED` / `NOT_CONFIGURED` | grey | grey ("not configured") |

## Document history

| Date | Notes |
|---|---|
| 2026-09-24 | Initial analysis and plan (architecture analysis by five read-only reviewers; this document) |
