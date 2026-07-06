# Multi-Queue Knowledge Base — Design Options

**Purpose:** Enable agents to open specific KB queues (HALAN, Gomla, Tasaheel, etc.) and work with multiple queues or multiple customers at the same time.

**Context:** AIVA today binds one account to one `corpus_id`. Chat RAG searches the entire corpus with no queue filter. The embedding layer already supports metadata filters (`vertical`, `interaction_type`, `issue_type`, `escalation`) in `search_similar`, but the chat pipeline does not pass them yet. KB chunks store queue/vertical in `payload_json.vertical`.

**Related data (main KB corpus):**

| Vertical | Primary chunks (`chunk_index = 0`) |
|----------|-----------------------------------:|
| CF | 172 |
| Pay | 101 |
| General | 98 |
| Tasaheel | 87 |
| Gomla | 69 |
| Halan | 41 |
| Saving | 39 |
| Gold | 31 |
| Commerce | 21 |
| Salary lending | 2 |
| Irrelevant Call | 1 |

Queue groups used in early exports:

| Queue | Verticals included |
|-------|-------------------|
| **HALAN** | CF, Pay, General, Halan, Saving, Gold, Commerce, Salary lending, Irrelevant Call |
| **Gomla** | Gomla |
| **Tasaheel** | Tasaheel |

---

## Two meanings of “many at once”

Before choosing an approach, clarify the product goal:

| Meaning | Description | Typical UX |
|---------|-------------|------------|
| **A. Many queues in one chat** | One conversation searches several KB scopes together | Multi-select queue checkboxes on a single chat |
| **B. Many customers in parallel** | Agent handles several cases at the same time | Multiple chat tabs, each with its own queue binding |

Most call-center setups need **both**: parallel tabs for separate customers, plus optional multi-queue search when a question spans products.

---

## Idea 1 — Queue toggles on a single chat session

**Summary:** Agent selects which queues are active for the current session. KB search runs only against those verticals.

```
Session
  active_queues: ["Tasaheel", "Gomla"]
       ↓
  KB search WITH vertical IN (...)
       ↓
  One LLM answer using filtered context
```

**Implementation sketch:**

- Add `active_queues` (JSON array) on `AIVA_chat_sessions`.
- Extend `search_similar` / `EmbeddingService.search` to accept a **list** of verticals (today: single `vertical` only).
- Pass active queues from `stream_rag_response` → `search_knowledge`.
- UI: checkboxes or chips for enabled queues.

| Pros | Cons |
|------|------|
| Small change on top of existing architecture | Too many open queues → noisy retrieval |
| One conversation, one coherent answer | Requires backend + UI work for multi-vertical filter |
| Reuses one corpus; no data migration | Agent must remember to enable the right queue |
| Aligns with filters already in `embedding_service` | Single `top_k` may under-represent a queue when many are active |

**Best for:** Agents who mostly handle one customer but occasionally need two related products (e.g. Halan Card + Tasaheel branch).

---

## Idea 2 — Queue groups (business / IVR mapping)

**Summary:** Agents pick **named queues** (HALAN, Gomla, Tasaheel), not raw verticals. Backend maps each queue to a set of verticals.

```
Agent selects: "HALAN"
       ↓
Backend expands: [CF, Pay, General, Halan, Saving, Gold, Commerce, ...]
       ↓
Search filtered to that set
```

**Implementation sketch:**

- Config table or JSON file: `queue_name → [verticals]`.
- Session stores `active_queues` as business names, not vertical slugs.
- Admin UI to edit mappings when new verticals are added.

| Pros | Cons |
|------|------|
| Matches call-center and IVR language | Mapping must be maintained when ingest adds verticals |
| Simpler agent UI (3–5 options vs 11) | Wrong mapping → missing or wrong scripts |
| Same as queue CSV exports already produced | Overlap between queues needs a documented rule |
| Easy to align with supervisor reporting by queue | “HALAN” is a large bucket — still noisy if unfiltered |

**Best for:** Production rollout where agents think in IVR queues, not CRM vertical codes.

---

## Idea 3 — Multiple sessions / tabs (parallel customers)

**Summary:** “Talk to many at once” = multiple chat sessions open simultaneously. Each session has its own queue binding and message history.

```
Agent workspace
├── Tab 1: Customer A — Queue: Tasaheel
├── Tab 2: Customer B — Queue: Gomla
└── Tab 3: Customer C — Queue: HALAN
```

**Implementation sketch:**

- No change to “one session = one scope” rule.
- UI: tabbed or split layout; list sessions filtered by `user_id` + `session_status = OPEN`.
- Optional: link session to `ticket_id` or call ID.

| Pros | Cons |
|------|------|
| Fits current `AIVA_chat_sessions` model | No cross-queue answer in one reply unless Idea 1 is added |
| Clean isolation per customer | More UI complexity (tabs, focus, notifications) |
| Easy to audit which queue was used per case | Agent context-switching overhead |
| Trainees can be limited to one tab at a time | Each tab still needs queue selector |

**Best for:** Live agent workflow with several active customers.

---

## Idea 4 — Multi-search merge and rerank

**Summary:** When multiple queues are active in one session, run retrieval per queue (or one `IN` query), merge results, dedupe, optionally rerank, then send one context block to the LLM.

```
For each active queue:
  search top_k_per_queue (e.g. 5)
       ↓
Merge + dedupe by external_parent_id
       ↓
Optional rerank by distance/score
       ↓
LLM context labeled [Tasaheel], [Gomla], ...
```

**Implementation sketch:**

- `top_k_per_queue = ceil(top_k / num_queues)` or fixed cap per queue.
- Dedupe: keep best score per `external_parent_id`.
- Prefix chunk text in context with queue label for the model.

| Pros | Cons |
|------|------|
| Fair representation across queues | Higher latency (N searches or heavier SQL) |
| Reduces one queue dominating `top_k` | Larger prompts → cost and token limits |
| Clear attribution in answers | More engineering (merge, rerank, tests) |
| Works well with Idea 1 + 2 | Tuning `top_k_per_queue` is operational work |

**Best for:** Sessions with 2–3 active queues where balanced retrieval matters.

---

## Idea 5 — Auto-router (classify then search)

**Summary:** Before search, classify the user message into likely queue(s). Search only those; agent can override manually.

```
User message
       ↓
Router (rules / keywords / small LLM)
       ↓
Suggested queues: ["Tasaheel"]
       ↓
Search (agent can add/remove queues)
```

**Implementation sketch:**

- Phase 1: keyword rules (تساهيل, فرع, جملة, كارت, أقساط, …).
- Phase 2: lightweight classifier or LLM call with vertical list.
- Store `suggested_queues` and `active_queues` on session for analytics.

| Pros | Cons |
|------|------|
| Less manual work for agents | Misclassification → wrong scripts |
| Good defaults for trainees | Extra latency and failure mode |
| Can pre-fill from IVR payload | Rules drift as KB grows |
| Improves Idea 1 when many queues exist | Needs override UI always |

**Best for:** Mature deployment after queue toggles exist; not as v1 alone.

---

## Idea 6 — Separate corpora per queue

**Summary:** Split KB into multiple `kb_corpus` rows (one per queue). Ingest each vertical export into its own corpus. Account or session selects which corpus(es) to search.

```
kb_corpus: halan-main, gomla, tasaheel
       ↓
Session: corpus_ids = [gomla, tasaheel]
       ↓
Search each corpus (or union)
```

**Implementation sketch:**

- Re-ingest CSVs per queue into separate corpora.
- `AIVA_accounts`: `corpus_id` → `corpus_ids` JSON or junction table.
- Search loops over corpora or uses partition + corpus filter.

| Pros | Cons |
|------|------|
| Hard isolation per product line | Migration and dual maintenance |
| Independent ingest/reindex per queue | Scripts spanning queues duplicated or split awkwardly |
| Different embed models per corpus (future) | Account model change |
| Strong access control per corpus | Overkill while `vertical` filter works |

**Best for:** Different teams owning KBs, different refresh SLAs, or strict compliance boundaries — not the default path today.

---

## Idea 7 — Ticket / IVR-linked sessions

**Summary:** Queue is chosen when the session starts — from IVR, CRM, or ticket — not only by agent manual toggle.

```
IVR: customer pressed 5 (Gomla)
       ↓
Create session with active_queues: ["Gomla"]
       ↓
Agent opens workspace → queue already set
```

**Implementation sketch:**

- `AIVA_chat_sessions`: `ticket_id`, `ivr_queue`, `active_queues`.
- API: `POST /chat/sessions` accepts optional `queue` / `ticket_id`.
- Widget or telephony integration passes queue on session create.

| Pros | Cons |
|------|------|
| Matches real call-center flow | Requires telephony/CRM integration |
| Reduces agent setup errors | Wrong IVR routing still wrong in AIVA |
| Strong audit trail (queue ← IVR ← ticket) | Depends on external systems |
| Supervisors can enforce queue per ticket type | More fields and API contract |

**Best for:** Integrated deployment with existing IVR and ticketing.

---

## Comparison matrix

| Idea | Effort | Data migration | Multi-customer | Multi-queue in one chat | Fits current stack |
|------|--------|----------------|------------------|-------------------------|-------------------|
| 1. Queue toggles | Low | None | Partial | Yes | **High** |
| 2. Queue groups | Low | Config only | Partial | Yes | **High** |
| 3. Multi-tab sessions | Medium | None | **Yes** | Per tab | **High** |
| 4. Merge/rerank | Medium | None | N/A | Yes (quality) | Medium |
| 5. Auto-router | Medium–High | None | Optional | Yes | Medium |
| 6. Separate corpora | High | **Yes** | Yes | Yes | Low |
| 7. IVR/ticket link | Medium | Schema + integrations | Yes | Yes | Medium |

---

## Recommended phased rollout

### Phase 1 — Foundation (low effort)

- Define queue group config (Idea 2): HALAN / Gomla / Tasaheel → vertical lists.
- Add `active_queues` JSON on `AIVA_chat_sessions` (Idea 1).
- Extend search API to accept `verticals: list[str]`.
- Wire RAG to pass session queues into search.
- UI: queue selector on chat (single session).

**Success criteria:** Agent enables Tasaheel only → answers cite Tasaheel scripts; enabling Gomla does not return CF chunks.

### Phase 2 — Parallel work (medium effort)

- Multi-tab session list in agent UI (Idea 3).
- Optional `ticket_id` on session.
- Session list shows active queue per tab.

**Success criteria:** Agent maintains 3 open sessions with different queues without cross-contamination.

### Phase 3 — Quality and automation (medium effort)

- Multi-search merge when multiple queues active (Idea 4).
- Keyword-based queue suggestion (Idea 5, rules only).
- Analytics: sessions by queue, retrieval hit rate by queue.

### Phase 4 — Integrations (as needed)

- IVR pre-selects queue on session create (Idea 7).
- LLM-based router if rules are insufficient (Idea 5).
- Separate corpora (Idea 6) only if isolation requirements justify it.

---

## Minimal schema additions

```sql
-- Chat session: which queues are in scope for KB search
ALTER TABLE AIVA_chat_sessions ADD (
  active_queues  JSON,           -- e.g. ["Tasaheel"] or ["HALAN","Gomla"]
  ivr_queue      VARCHAR2(64),  -- optional: raw IVR selection
  ticket_id      NUMBER          -- optional: link to AIVA_tickets
);

-- Optional catalog (alternative: JSON config file in repo)
CREATE TABLE AIVA_kb_queues (
  id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id     NUMBER NOT NULL,
  name           VARCHAR2(64) NOT NULL,   -- HALAN, Gomla, Tasaheel
  verticals_json JSON NOT NULL,           -- ["CF","Pay",...]
  corpus_id      RAW(16) NOT NULL,
  UNIQUE (account_id, name)
);
```

---

## API / code touchpoints (AIVA-V2)

| Layer | File / component | Change |
|-------|------------------|--------|
| Search | `embedding_service/db/repo.py` | `vertical` → `verticals: list[str]` with `IN` filter |
| Service | `embedding_service/service.py` | Expose `verticals` on `search()` |
| RAG | `backend/services/rag.py` | Read session queues; pass to `search_knowledge` |
| Chat | `backend/routers/chat.py` | CRUD for `active_queues`; create session with queue |
| Schema | `backend/services/chat_schema.py` | Migrate `active_queues` column |
| UI | Agent chat widget | Queue chips + multi-tab sessions |

**Example search signature (target):**

```python
embedding_svc.search(
    corpus_id,
    query,
    verticals=["Tasaheel", "Gomla"],  # expanded from active_queues
    top_k=10,
)
```

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Agent forgets to enable correct queue | Default from IVR/ticket; suggest queue from message (Idea 5) |
| HALAN bucket too large | Sub-filters later (interaction_type, escalation) already in repo |
| Skipped / bad chunks in results | Filter `answer_status != 'skipped'` in search WHERE clause |
| Split script segments (`chunk_index > 0`) | Prefer `chunk_index = 0` in search or group by parent in UI |
| Trainee searches entire KB | Role policy: trainees limited to one queue |

---

## Decision checklist

Use this when picking an approach:

1. Do agents need **one chat** to search **multiple queues at once**? → Ideas 1, 2, 4  
2. Do agents need **several customers open simultaneously**? → Idea 3  
3. Should queue come from **IVR/ticket** automatically? → Idea 7  
4. Are queues owned by **different teams with separate ingest**? → Consider Idea 6  
5. Is v1 acceptable with **manual queue selection only**? → Start Phase 1  

---

## Document history

| Date | Author | Notes |
|------|--------|-------|
| 2026-07-06 | AIVA design discussion | Initial options from KB vertical export and current RAG architecture |
