# 4b. Rhodey's Four Tiers of Intelligence

Rhodey's intelligence isn't a single model or pipeline — it's a **layered architecture** where each tier adds a deeper capability. Lower tiers handle the basics; higher tiers discover connections, remember context, and act proactively.

```
Tier 4  │ Session Working Memory ─── Active anchor, proactive signals, source heuristics
Tier 3  │ Memory & Graph Intelligence ── Hindsight, serendipity, temporal patterns, KG traversal
Tier 2  │ Context Hydration ──────── Multi-source parallel fetch, TTL caching, semantic selection
Tier 1  │ Pulse Orchestration ────── Intent classification, Action Planner routing, Google sync
```

All four tiers are always active. A single `interrogate_brain()` call touches every tier in sequence.

---

## Tier 1: Pulse Orchestration & Basic Processing

**Purpose:** Route incoming messages, run the briefing cycle, and execute basic data operations.

**Core files:** `core/webhook/handler.py`, `core/webhook/classify.py`, `core/webhook/dispatch.py`, `core/actions/planner.py`, `core/actions/executor.py`, `core/pulse/briefing.py`

**What it does:**

1. **Intent classification** (`classify.py`) — Every incoming Telegram message is classified by Gemini Flash Lite into one of 11 intents: `TASK`, `NOTE`, `COMPLETION`, `PROJECT_UPDATE`, `QUERY`, `DAILY_BRIEF`, `DELEGATE`, `DECLARE_PRACTICE`, `ROLE_UPDATE`, `NOISE`, or `CLARIFICATION_NEEDED`. The classifier has explicit heuristics (URLs → TASK, schedule ranges → QUERY, "good morning" → DAILY_BRIEF). Supports `secondary_actions` array for multi-intent messages.

2. **Unified Action Planner** (`planner.py` + `executor.py`) — All task/note/completion operations route through a single typed Action pipeline:
   - `plan_actions()`: Single LLM call resolves user intent into typed `Action` objects (create_task, close_task, reschedule, cancel_recurring, etc.) using a multi-source candidate pool (active tasks + recurring tasks + 14-day calendar window)
   - `execute_planned_actions()`: Executes actions through `create_task_direct()`, `create_note_direct()`, `update_task_status()` — direct DB operations with entity resolution BEFORE creation
   - URL quarantine at ingress routes bare URLs directly to resources table with no LLM call

3. **Pulse briefing cycle** (`briefing.py`) — A scheduled AI cycle (3-7x daily) that:
   - Fetches active tasks, calendar, people, projects via parallel `asyncio.gather`
   - Assembles context (30+ fields across 2 parallel phases)
   - Generates a structured briefing via a single LLM call (no agent loop)
   - Syncs with Google, delivers via Telegram + FCM push
   - Write-behind pattern: all DB writes happen AFTER briefing generation

4. **Decision Pulse** — A no-AI pulse variant that surfaces pending approvals (email tasks, call items, WhatsApp messages, graph nodes/edges) as inline keyboards with one-tap approve/reject.

5. **Google sync** — Bi-directional sync with Google Calendar (event blocks) and Google Tasks (checklist). 404 auto-heal for externally deleted events. Priority-based event prefixes.

**Key behaviors:**
- Dedup via `processed_updates` table with UNIQUE constraint + `dedup_key` hash
- Authorization check against `TELEGRAM_CHAT_ID` + HMAC PULSE_SECRET
- All Tier 1 operations gracefully degrade on failure
- Enrichment (graph edges, entities, embeddings) queued via `pending_enrichment_jobs` to survive Vercel cold kills

---

## Tier 2: Context Hydration Engine

**Purpose:** Pre-compute and cache context from multiple data sources so every downstream query starts with rich, token-optimized state.

**Core files:** `core/pulse/context.py`, `core/lib/redis_cache.py`

**What it does:**

`ContextProvider` (`core/pulse/context.py:48`) is a singleton that maintains **6 TTL caches** backed by Upstash Redis:

| Cache Key | TTL | Data |
|-----------|-----|------|
| `rhodey:cache:tasks` | 30s | Active tasks with priority, project, reminder |
| `rhodey:cache:projects` | 300s | Active project names and org tags |
| `rhodey:cache:people` | 300s | People names and strategic weights |
| `rhodey:cache:calendar` | 300s | Today's Google + Outlook events |
| `rhodey:cache:recent_tasks` | 60s | Tasks completed in the last 24h |
| `rhodey:cache:calendar_range:{start}:{end}:{max_days}` | 120s | Date-range calendar queries |

Each cache follows a **dual-layer pattern**:
1. In-memory `SimpleCache` with TTL (fastest, process-local)
2. Redis fallback via `cache_get`/`cache_set` (survives cold starts across serverless instances)
3. If Redis is unavailable → silently degrades to in-memory only (no crashes)

**Key functions:**

- `hydrate_tasks_context()` — Semantic selection with hard safeguards: overdue/urgent tasks always included, remaining tasks ranked by recency/priority, capped at 4KB. Cache invalidation on all status-change paths (done/cancelled).
- `hydrate_memories_context()` — pgvector hybrid search with recency decay + importance weighting
- `get_range_calendar_events()` — Parallel fetch from Google + Outlook, capped at 14 days, events tagged `[PAST]` in Python
- `get_cross_referenced_context()` — Merges pgvector memory results with graph traversal, dynamically tagging memories that mention known entities
- `get_email_context()` / `get_whatsapp_context()` — Hybrid vector search on email/WhatsApp tables
- `get_pending_decisions_context()` — Aggregates unapproved items from messages, pending_nodes, and pending_graph_edges
- `get_resources_context()` / `get_practices_context()` — Resource/practice vector search and status lookup

**Graceful degradation:** Every method has a `try/except` wrapper. One failing data source never blocks others.

---

## Tier 3: Memory & Graph Intelligence

**Purpose:** Retrieve, connect, and surface insights from the system's historical knowledge — memories, relationships, and patterns.

**Core files:** `core/retrieval/search.py`, `core/retrieval/pipeline.py`, `core/retrieval/ranking.py`, `core/retrieval/ppr.py`, `core/pulse/memory.py`, `core/pulse/graph.py`, `core/skills/backfill_graph.py`, `core/context/pipeline.py`

**What it does:**

### 3a. Associative Retrieval Engine

The primary retrieval path uses `associative_retrieve()` (`core/retrieval/search.py`) — a 7-signal ranking pipeline:

1. **Query Analysis** — Parallel via `asyncio.gather()`: Gemini Flash Lite entity extraction (Redis-cached 1h), lexical word n-gram splitting (GIN trigram ~5ms), and query embedding (Redis-cached 24h).
2. **Graph Traversal** — Matched phrase nodes seed `personalized_pagerank()` across the bounded subgraph (~50ms). Alias edges bridge synonymous labels.
3. **7-Signal Ranking** — Memories ranked by semantic similarity, PPR score, recency, importance, project boost, specificity (node degree), and person_boost.
4. **Aggregation** — Passages aggregated to memories via nested PostgREST joins. Results deduplicated and returned as `ExplainableBundle`.

**Performance:** Cold path 3.5–5.0s, warm path 1.8–3.5s (Redis eliminates LLM extraction and embedding on cache hits).

### 3b. Knowledge Graph Traversal

`hybrid_search_graph()` (`graph.py`) walks the graph edges (`DISCUSSED_WITH`, `WORKS_AT`, `CLIENT_OF`, etc.) to surface connections between tasks, people, projects, and memories.

**Graph integrity safeguards:**
- **Guard A** — Orphaned `BELONGS_TO` edges cleaned before insert (by `metadata->>task_id`)
- **Guard B** — Text-anchoring validation drops hallucinated labels not found in source text
- **HITL** — All edges flow through `pending_graph_edges` approval table. New person/project/organization nodes gated through `pending_nodes`.
- **Guard D** — Label-drift dedup: ILIKE exact + fuzzy fallback across all statuses before insert
- **5 node types** (person, organization, project, place, animal) + **16 edge types** — all through HITL

### 3c. Forward Indexing

Every new memory triggers `schedule_index_memory()` — chunks text into passages, extracts entities, embeds phrase nodes, builds link tables. Runs via `pending_retrieval_index_jobs` queue + sentinel piggyback.

### 3d. Context Registry (`core/context/`)

6 per-strategy configs (`PRE_FLIGHT`, `BRIEFING`, `HINDSIGHT`, `HYDRATE_TASKS`, `HYDRATE_MEMORIES`, `BRAIN_SYNTH`) with entity-grounding gates (hard/soft/none). Neutral context penalty (0.5x). Structured audit logging for rejection reasons.

### 3e. Hindsight, Serendipity & Temporal Patterns

- `retrieve_hindsight_memories()` — Routes through associative retrieval for memories related to active tasks
- `serendipity_engine()` — Cross-domain keyword bridges, people-in-resources, temporal serendipity
- `detect_temporal_patterns()` — Timehop-style memories from same month/day across all years

### 3f. After-Action Report

Runs nightly: queries completed + open tasks, sends summary to Gemini for 1-2 sentence reflection, saves as `memory_type: 'reflection'`.

---

## Tier 4: Session Working Memory

**Purpose:** Maintain conversational context across messages, proactively surface relevant signals, and intelligently select data sources per query.

**Core files:** `core/webhook/dispatch.py:575` (`interrogate_brain()`), `core/lib/conversation.py`

**What it does:**

### 4a. Active Anchor

Each conversation session has an `active_anchor` — structured JSONB with `{id, name, type, last_action, last_task_id, last_project_id, last_org_id, last_summary_snippet, last_mentioned_at}`. Stored on `conversation_threads` with per-session timeout.

**Resolution pipeline** (runs on every QUERY):
1. LLM rewrites the query to resolve pronouns via anaphora resolution
2. Extracts the primary entity from the resolved query
3. Matches against `graph_nodes` (exact → ilike → edge-count tiebreaker)
4. Updates the session's active anchor

### 4b. Proactive Signals

In parallel with every query response, `check_proactive_signals()` checks pending drafts, unapproved messages, and unapproved graph nodes mentioning the anchor. All checks run with a 1.5-second timeout.

### 4c. Source Selection Heuristics

Three boolean flags gate which of the 14 data sources are fetched:
- `is_schedule` — calendar, schedule, meeting, today, week, when → Calendar, tasks, people
- `is_comms` — email, message, said, told, chat, WhatsApp → Emails, WhatsApp, people
- `is_action` — task, todo, block, status, progress, done → Tasks, tactical map, serendipity

If **none** match → all 14 sources fetched (catch-all).

### 4d. Time-Aware Formatting & Streaming

Events tagged `[PAST]` in Python. Queries use **streaming** Gemini responses for faster time-to-first-token. CONTEXT_SECTION_RULES annotation prevents confusion between ACTIVE TASKS and RECENTLY COMPLETED tasks.

### 4e. Decision Audit (`/why`)

Structured audit logging for 4 decision stages (classification, routing, context_registry, retrieval). Conversational `/why` command explains the last bot response with per-stage reasoning.

---

## How the Tiers Stack

A single Telegram query flows through all four tiers:

```
User: "What's happening with Solvstrat this week?"

Tier 1 (handler.py) ──── Intent classification → QUERY
                            │
Tier 2 (dispatch.py) ──── Parallel context fetch (14 sources in 3 groups)
                            │  └─ TTL caches hit Redis for tasks/people
                            │
Tier 3 (retrieval) ─────── Associative retrieval (7-signal ranking)
                            │  └─ LLM entity extraction + lexical phrase matching
                            │  └─ PPR graph traversal + alias bridges
                            │  └─ Context registry gates
                            │
Tier 4 (dispatch.py) ──── Anaphora ("Solvstrat" is the anchor)
                            │  └─ Proactive signal check for Solvstrat drafts
                            │  └─ [PAST] tagging on calendar events
                            │  └─ Streaming Gemini response
                            │
                            ▼
                    Gemini responds (streaming, 600 token cap)
                            │
                            ▼
                    Append proactive signals → Telegram reply
```

Each tier is independent and gracefully degrades. A Redis outage only reduces cache speed, not correctness. A memory embedding failure doesn't block calendar queries.
