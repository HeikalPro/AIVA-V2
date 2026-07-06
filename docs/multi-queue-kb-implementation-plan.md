# Idea 2 — Queue Groups: Implementation Plan

**Goal:** Agents choose business queues (**HALAN**, **Gomla**, **Tasaheel**). Backend maps each queue to CRM verticals and filters KB search accordingly. One corpus stays; no re-ingest.

**Aligns with:** IVR tree (Card / Halan Inbound / Gomla / …) and existing `payload_json.vertical` on `kb_chunk`.

---

## 1. Target behavior

```
Agent UI:  [x] HALAN  [ ] Gomla  [ ] Tasaheel
                    │
                    ▼
Session.active_queues = ["HALAN"]
                    │
                    ▼
Resolve → verticals = [CF, Pay, General, Halan, Saving, Gold, Commerce, Salary lending, Irrelevant Call]
                    │
                    ▼
embedding_svc.search(corpus_id, query, verticals=[...], top_k=10)
                    │
                    ▼
RAG answer + sources only from those verticals
```

**Rules:**

| Rule | Decision |
|------|----------|
| Default when session created | All queues enabled (backward compatible) **or** account-configured default (e.g. HALAN only) |
| No queue selected | Block send with error, or fall back to all queues (pick one in Phase 1) |
| Multiple queues selected | Union of verticals; single search with `IN` filter |
| Demo corpus | Excluded from queue groups (agents use main KB account only) |

---

## 2. Queue group catalog (v1 seed)

Store on the **main KB corpus** (`091B8D61C54645EF86DF0D78E0B9AE0C`). Adjust labels to match IVR exactly.

```json
{
  "queue_groups": {
    "HALAN": {
      "label": "Halan",
      "ivr_hint": "Card / Halan Inbound / Cash / Elite / …",
      "verticals": [
        "CF",
        "Pay",
        "General",
        "Halan",
        "Saving",
        "Gold",
        "Commerce",
        "Salary lending",
        "Irrelevant Call"
      ]
    },
    "Gomla": {
      "label": "Gomla",
      "ivr_hint": "IVR press 5",
      "verticals": ["Gomla"]
    },
    "Tasaheel": {
      "label": "Tasaheel",
      "ivr_hint": "Branch / تساهيل",
      "verticals": ["Tasaheel"]
    }
  }
}
```

**Maintenance:** When a new vertical appears in ingest, add it to the right queue in config (or an admin UI later). Unknown verticals not in any group are only searchable when “all queues” is on.

---

## 3. Where to store config

| Option | Pros | Cons | Recommendation |
|--------|------|------|----------------|
| **A. `kb_corpus.config_json.queue_groups`** | Lives with KB; one source of truth per corpus | Edit via corpus patch API | **v1 — use this** |
| B. `AIVA_accounts` JSON column | Per-account overrides | Duplication if one corpus per org | Phase 2 if needed |
| C. New `AIVA_kb_queues` table | Admin UI, versioning | More schema work | Phase 3 |

**Session state:** `AIVA_chat_sessions.active_queues` — JSON array of queue keys, e.g. `["HALAN","Gomla"]`.

---

## 4. Implementation phases

### Phase 1 — Backend foundation (no UI yet)

**4.1 Corpus config**

- Extend `CorpusConfig` (or validate extra keys in `config_json`) with optional `queue_groups: dict[str, QueueGroupConfig]`.
- Seed main corpus via script or `PATCH /api/corpora/{id}` with `queue_groups` above.
- Add `backend/services/kb_queue_groups.py`:
  - `list_queue_groups(corpus_config) -> list[QueueGroupOut]`
  - `resolve_verticals(queue_groups, active_queue_keys: list[str]) -> list[str]`
  - `validate_active_queues(keys, queue_groups) -> list[str]` (raises if invalid)

**4.2 Database migration**

In `backend/services/chat_schema.py` (same pattern as message columns):

```sql
ALTER TABLE AIVA_chat_sessions ADD (active_queues CLOB);  -- JSON array, nullable
```

- `NULL` or `[]` = search all verticals (backward compatible during rollout).
- After UI ships, optionally require at least one queue.

**4.3 Search layer — multi-vertical filter**

Files:

| File | Change |
|------|--------|
| `embedding_service/db/repo.py` | `search_similar(..., verticals: list[str] | None = None)` — if set, `JSON_VALUE(...) IN (...)` bind list |
| `embedding_service/db/repository.py` | Pass through `verticals` |
| `embedding_service/service.py` | `search(..., verticals=...)`; keep `vertical=` as sugar for single value |
| `backend/services/rag.py` | `search_knowledge(..., verticals=...)`; `stream_rag_response(..., verticals=...)` |

**Oracle note:** Use something like:

```sql
JSON_VALUE(c.payload_json, '$.vertical' RETURNING VARCHAR2(256)) IN (:v0, :v1, ...)
```

Cap list length (e.g. 20 verticals) to avoid bind explosion.

**Optional quality filters (same PR or follow-up):**

- `chunk_index = 0` only (avoid duplicate split segments in RAG).
- Exclude `answer_status = 'skipped'`.

**4.4 Chat API**

| Endpoint | Change |
|----------|--------|
| `GET /api/corpora/{corpus_id}/queue-groups` | New — returns groups for account’s corpus (agent-readable) |
| `POST /api/chat/sessions` | Body: `active_queues?: string[]` — validate against corpus config |
| `PATCH /api/chat/sessions/{id}/queues` | New — update `active_queues` mid-session |
| `GET /api/chat/sessions` | Include `active_queues` in `SessionOut` |
| `POST /api/chat/sessions/{id}/messages` | Load session → resolve verticals → pass to `stream_rag_response` |

**Schemas (`backend/schemas/chat.py`):**

```python
class SessionCreate(BaseModel):
    account_id: int
    active_queues: list[str] | None = None  # e.g. ["HALAN"]

class SessionOut(BaseModel):
    ...
    active_queues: list[str] = []

class SessionQueuesUpdate(BaseModel):
    active_queues: list[str] = Field(min_length=1)
```

**4.5 RAG wiring**

In `send_message` / `stream_rag_response`:

1. Load account → `corpus_id`.
2. Load corpus config → `queue_groups`.
3. Read `session.active_queues` (or default).
4. `verticals = resolve_verticals(queue_groups, active_queues)` or `None` if empty/all.
5. `search_knowledge(..., verticals=verticals)`.

**4.6 Tests**

- Unit: `resolve_verticals` — HALAN only, Gomla+Tasaheel, invalid key.
- Integration: search with `verticals=["Gomla"]` returns no CF chunks.
- Chat: session with `["Tasaheel"]` → sources only from Tasaheel parent IDs.

**Phase 1 exit criteria:** API can create session with queues; message search respects filter; existing sessions without `active_queues` still work (full corpus).

---

### Phase 2 — Agent UI (AIVA-V2-UI)

**4.7 Types & API client**

- `ChatSession.active_queues`, `QueueGroup` type.
- `useQueueGroups(corpusId)` → `GET .../queue-groups`.
- `useUpdateSessionQueues(sessionId)`.

**4.8 ChatPage UX**

- When account selected, fetch queue groups for account’s `corpus_id`.
- **Queue selector:** 3 checkboxes/chips (HALAN, Gomla, Tasaheel).
- On **new session:** pass `active_queues` to `POST /api/chat/sessions`.
- On **existing session:** show current queues; changing calls `PATCH .../queues`.
- Show active queues in session list label: `Agent · HALAN+Gomla · 3 messages`.
- Disable send if zero queues selected (if product rule requires ≥1).

**4.9 Widget (AIVA-widget)** — if agents use widget

- Same queue chips; env or props for default queues.
- Pass `active_queues` on session create.

**Phase 2 exit criteria:** Agent toggles Gomla only; answers and KB source links come from Gomla vertical only.

---

### Phase 3 — Hardening & ops

| Item | Action |
|------|--------|
| Seed script | `scripts/seed_queue_groups.py` — patch main corpus config |
| Logging | Log `active_queues` + resolved `verticals` on each message (debug) |
| Analytics | Count sessions/messages by queue (supervisor dashboard later) |
| Trainee policy | Optional: role flag limiting allowed queues |
| Docs | Update agent runbook with queue ↔ IVR mapping |

---

### Phase 4 — Future (out of v1 scope)

- IVR passes queue on `POST /sessions` (`ivr_queue: "Gomla"`).
- Per-account override of `queue_groups`.
- Admin UI to edit groups without corpus JSON patch.
- Per-queue `top_k` merge (Idea 4) if HALAN+Gomla together is noisy.

---

## 5. File checklist

### Backend (AIVA-V2)

| File | Task |
|------|------|
| `embedding_service/models/corpus_config.py` | Optional `QueueGroupConfig` model |
| `embedding_service/db/repo.py` | `verticals` list in `search_similar` |
| `embedding_service/service.py` | `search(verticals=...)` |
| `backend/services/kb_queue_groups.py` | **New** — resolve & validate |
| `backend/services/chat_schema.py` | `active_queues` column |
| `backend/services/rag.py` | Pass `verticals` into search |
| `backend/schemas/chat.py` | Session create/out/update queues |
| `backend/routers/chat.py` | Create/patch session, wire RAG |
| `backend/routers/corpora.py` | `GET .../queue-groups` |
| `scripts/seed_queue_groups.py` | **New** — seed config |

### Frontend (AIVA-V2-UI)

| File | Task |
|------|------|
| `src/types/api.ts` | Queue types |
| `src/hooks/useChat.ts` | Queue groups hook, patch queues |
| `src/pages/ChatPage.tsx` | Queue selector UI |

### Tests

| File | Task |
|------|------|
| `tests/test_kb_queue_groups.py` | **New** |
| `tests/test_search_verticals.py` or embedding tests | Multi-vertical search |

---

## 6. Default & backward compatibility

| Scenario | Behavior |
|----------|----------|
| Old session, `active_queues` NULL | Search **entire corpus** (current behavior) |
| New session, UI sends `["HALAN"]` | Search HALAN verticals only |
| New session, UI sends all three | Union of all verticals ≈ full corpus (minus unmapped) |
| Invalid queue key in API | `400 Bad Request` |

After UI is live, consider changing default new-session to `["HALAN"]` only if product wants narrower search by default.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| New vertical not in any group | Lint script on export/ingest; alert if unmapped verticals exist |
| HALAN bucket too large | Later: sub-filters (`interaction_type`, `escalation`) already in repo |
| Agent forgets to switch queue | Phase 4: IVR pre-select; optional keyword hint in UI |
| Split chunks (`chunk_index > 0`) pollute RAG | Add `chunk_index = 0` filter in search |
| Oracle `IN` bind limits | Max ~20 verticals per query — HALAN has 9, safe |

---

## 8. Effort estimate

| Phase | Scope | Estimate |
|-------|--------|----------|
| Phase 1 | Backend + search + API | 2–3 days |
| Phase 2 | AIVA-V2-UI (+ widget if needed) | 1–2 days |
| Phase 3 | Seed, logging, docs | 0.5 day |
| **Total v1** | | **~4–6 days** |

---

## 9. Order of work (developer sequence)

1. Add `queue_groups` to corpus config + seed script.
2. Implement `kb_queue_groups.resolve_verticals`.
3. Extend `search_similar` with `verticals` list.
4. Add `active_queues` column + migration.
5. Wire RAG + `send_message`.
6. Add corpus queue-groups endpoint + session create/patch.
7. Manual test with curl/Postman (Gomla-only query).
8. UI queue chips + session create/update.
9. End-to-end QA with known parent IDs per vertical.

---

## 10. Quick manual test script (after Phase 1)

```bash
# 1. Seed queue groups on corpus
python scripts/seed_queue_groups.py

# 2. Create session with Gomla only
POST /api/chat/sessions  { "account_id": 3, "active_queues": ["Gomla"] }

# 3. Ask a CF-specific question — should NOT return CF scripts
POST /api/chat/sessions/{id}/messages  { "message_text": "حالا كريديت على الكارت" }

# 4. Ask Gomla question — should hit Gomla chunks
POST /api/chat/sessions/{id}/messages  { "message_text": "أوردر ملغي جملة" }
```

---

## Document history

| Date | Notes |
|------|-------|
| 2026-07-06 | Implementation plan for Idea 2 (queue groups) |
