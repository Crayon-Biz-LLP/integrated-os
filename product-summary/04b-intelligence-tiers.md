# 4b. Rhodey's Four Tiers of Intelligence

Rhodey's intelligence isn't a single model or pipeline — it's a **layered architecture** where each tier adds a deeper capability. Lower tiers handle the basics; higher tiers discover connections, remember context, and act proactively.

```
Tier 4  │ Session Working Memory ─── Active anchor, proactive signals, source heuristics
Tier 3  │ Memory & Graph Intelligence ── Hindsight, serendipity, temporal patterns, KG traversal
Tier 2  │ Context Hydration ──────── Multi-source parallel fetch, TTL caching, semantic selection
Tier 1  │ Pulse Orchestration ────── Intent classification, task/note/completion routing, Google sync
```

All four tiers are always active. A single `interrogate_brain()` call touches every tier in sequence.

---

## Tier 1: Pulse Orchestration & Basic Processing

**Purpose:** Route incoming messages, run the briefing cycle, and execute basic data operations.

**Core files:** `core/webhook/handler.py`, `core/webhook/classify.py`, `core/webhook/dispatch.py`, `core/pulse/engine.py`

**What it does:**

1. **Intent classification** (`classify.py`) — Every incoming Telegram message is classified by Gemini Flash Lite into one of 9 intents: `TASK`, `NOTE`, `COMPLETION`, `QUERY`, `DAILY_BRIEF`, `DELEGATE`, `DECLARE_PRACTICE`, `NOISE`, or `CLARIFICATION_NEEDED`. The classifier is a 40-line prompt with explicit heuristics (URLs → TASK, schedule ranges → QUERY, good morning → DAILY_BRIEF).

2. **Task/Note/Completion routing** — The `route_by_intent()` dispatcher sends each intent to its dedicated handler:
   - `handle_confident_task()` → raw_dump insert, inline processing, graph edges
   - `handle_confident_note()` → embed + store to memories
   - `handle_confident_completion()` → LLM match to open task, close + Google sync

3. **Pulse briefing cycle** (`engine.py`) — A scheduled AI cycle (5x daily weekdays, 2x weekends) that:
   - Fetches active tasks, calendar, people, pending decisions
   - Runs 5 parallel AI agents (Compass, Schedule, Project, Task, Serendipity)
   - Generates a structured briefing, syncs with Google
   - Delivers via Telegram with inline keyboard approvals

4. **Decision Pulse** — A no-AI pulse variant that surfaces pending approvals (email tasks, call items, WhatsApp messages, graph nodes) as inline keyboards with one-tap approve/reject.

5. **Google sync** — Bi-directional sync with Google Calendar (event blocks) and Google Tasks (checklist).

**Key behaviors:**
- Dedup via `processed_updates` table with UNIQUE constraint
- Authorization check against `TELEGRAM_CHAT_ID`
- 60-second Vercel serverless timeout matches `LLM_TIMEOUT` settings
- All Tier 1 operations gracefully degrade on failure

---

## Tier 2: Context Hydration Engine

**Purpose:** Pre-compute and cache context from multiple data sources so every downstream query starts with rich, token-optimized state.

**Core files:** `core/pulse/context.py`, `core/lib/redis_cache.py`

**What it does:**

`ContextProvider` (`core/pulse/context.py:48`) is a singleton that maintains **5 TTL caches** backed by Upstash Redis:

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

- `hydrate_tasks_context()` — Semantic selection with hard safeguards: overdue/urgent tasks always included, remaining tasks ranked by recency/priority, capped at 4KB
- `hydrate_memories_context()` — pgvector hybrid search with recency decay + importance weighting
- `get_range_calendar_events()` — Parallel fetch from Google + Outlook, capped at 14 days, events tagged `[PAST]` in Python
- `get_cross_referenced_context()` — Merges pgvector memory results with graph traversal, dynamically tagging memories that mention known entities
- `get_email_context()` / `get_whatsapp_context()` — Hybrid vector search on email/WhatsApp tables
- `get_pending_decisions_context()` — Aggregates unapproved items from messages, messages, and messages
- `get_resources_context()` / `get_practices_context()` — Resource/practice vector search and status lookup

**Graceful degradation:** Every method has a `try/except` wrapper. One failing data source never blocks others.

---

## Tier 3: Memory & Graph Intelligence

**Purpose:** Retrieve, connect, and surface insights from the system's historical knowledge — memories, relationships, and patterns.

**Core files:** `core/retrieval/search.py`, `core/retrieval/pipeline.py`, `core/retrieval/ranking.py`, `core/retrieval/ppr.py`, `core/pulse/memory.py`, `core/pulse/graph.py`, `core/skills/backfill_graph.py`

**What it does:**

### 3a. Associative Retrieval Engine

The primary retrieval path uses `associative_retrieve()` (`core/retrieval/search.py`) — a 7-signal ranking pipeline that replaced the legacy pgvector-only `match_memories_hybrid` RPC:

1. **Query Analysis** — Parallel via `asyncio.gather()`: Gemini Flash Lite entity extraction (Redis-cached 1h), lexical word n-gram splitting (GIN trigram ~5ms), and query embedding (Redis-cached 24h).
2. **Graph Traversal** — Matched phrase nodes seed `personalized_pagerank()` across the bounded subgraph (~50ms). Alias edges bridge synonymous labels.
3. **7-Signal Ranking** — Memories ranked by semantic similarity, PPR score, recency, importance, project boost, specificity (node degree), and person_boost. Configurable weights in `core/retrieval/ranking.py`.
4. **Aggregation** — Passages aggregated to memories via nested PostgREST joins. Results deduplicated and returned as `ExplainableBundle`.

**Performance:** Cold path 3.5–5.0s, warm path 1.8–3.5s (Redis eliminates LLM extraction and embedding on cache hits).

### 3b. Knowledge Graph Traversal

`hybrid_search_graph()` (`graph.py`) walks the graph edges (`BELONGS_TO`, `MENTIONS`, `RELATED_TO`, `AUTHORED`, `INVOLVES`) to surface connections between tasks, people, projects, and memories. Results feed the tactical map — a structural view of how entities connect.

**Graph integrity safeguards:**
- **Guard A** — Orphaned `BELONGS_TO` edges cleaned before insert (by `metadata->>task_id`)
- **Guard B** — Text-anchoring validation drops hallucinated labels not found in source text
- **HITL** — New person/project/organization nodes gated through `pending_graph_nodes` for Danny's approval via Decision Pulse

### 3c. Forward Indexing

Every new memory write triggers `schedule_index_memory()` (`core/retrieval/pipeline.py`) — chunks text into passages (512-char sliding windows), extracts entities via Gemini Flash Lite, embeds phrase nodes, and builds link tables. Runs at concurrency 3 (`asyncio.Semaphore(3)`). 470 production memories indexed across all types.

### 3d. Hindsight Retrieval

`retrieve_hindsight_memories()` (`memory.py:104-170`) routes through `search_memories_compat()`, which directs to `associative_retrieve()` when enabled (all flags ON):
1. For active tasks with `reminder_at` → fetches memories with semantic similarity to the task title
2. For the current query → fetches matching memories
3. Results are merged, deduplicated, and trimmed to `top_k`

### 3e. Serendipity Engine

`serendipity_engine()` (`memory.py:271-320`) hunts for non-obvious connections across domains:
- **Cross-domain keyword bridges** — Words >4 chars appearing in 2+ org tags
- **People-in-resources** — Person names in resource titles/descriptions not explicitly linked by graph edges
- **Temporal serendipity** — Resources and memories created on the same day
- Results sampled to max 5 paths to protect token budget

### 3f. Temporal Pattern Detection

`detect_temporal_patterns()` queries memories from the same month/day across all years (Timehop-style). Results deduplicated, capped at 5, injected into briefing context.

### 3g. After-Action Report

Runs nightly (hour >= 20 IST): queries completed + open tasks, sends summary to Gemini for a 1-2 sentence reflection, saves as `memory_type: 'reflection'`.

---

## Tier 4: Session Working Memory

**Purpose:** Maintain conversational context across messages, proactively surface relevant signals, and intelligently select data sources per query.

**Core files:** `core/webhook/dispatch.py:575` (`interrogate_brain()`), `core/pulse/proactive.py`, `core/lib/conversation.py`

**What it does:**

### 4a. Active Anchor

Each conversation session has an `active_anchor` — the primary person, project, or organization being discussed. Stored in `conversations.metadata` with a **15-minute timeout**.

**Resolution pipeline** (runs on every QUERY):
1. LLM rewrites the query to resolve pronouns via anaphora resolution
2. Extracts the primary entity from the resolved query
3. Matches against `graph_nodes` (exact → ilike → edge-count tiebreaker)
4. Updates the session's active anchor

The anchor **scopes** all subsequent context:
- `hybrid_search_graph()` receives the anchor's node ID (finds edges from this entity)
- `check_proactive_signals()` checks drafts/tasks/messages mentioning the anchor
- Serendipity engine seeds with the anchor's graph node

### 4b. Proactive Signals

In parallel with every query response, `check_proactive_signals()` (`proactive.py`:4) checks:
- `email_drafts` for pending drafts mentioning the anchor
- `messages` for suggested tasks linked to the anchor
- `messages` for unapproved messages mentioning the anchor

All checks run with a **1.5-second `asyncio.wait_for`** timeout. Results append as a 💡 note to the Telegram response. If no signals found, nothing shown.

### 4c. Source Selection Heuristics

Three boolean flags gate which of the 14 data sources are fetched:

| Flag | Triggers | Sources Fetched |
|------|----------|----------------|
| `is_schedule` | calendar, schedule, meeting, today, week, when | Calendar, tasks, people, tactical map |
| `is_comms` | email, message, said, told, chat, WhatsApp, contact | Emails, WhatsApp, people, tactical map |
| `is_action` | task, todo, block, status, progress, done, completed | Tasks, tactical map, serendipity, hindsight |

If **none** match → all 14 sources fetched (catch-all intelligence). Each source uses `safe_fetch()` for graceful degradation.

### 4d. Time-Aware Calendar Formatting

Events from `get_range_calendar_events()` are pre-tagged with `[PAST]` in Python if their start time is before current IST time. The prompt includes `CURRENT TIME: {IST}`. The LLM is instructed to separate past from upcoming events.

### 4e. Output Format Enforcement

The `interrogate_brain()` prompt uses a **strict output template**:
1. Answer first (schedule events or direct response as bullet list)
2. `**Context:**` section second (optional, 1-3 sentences)
3. No invented headings ("Immediate Priorities", "Today's Bottleneck")
4. Stop immediately after Context section
5. Max 600 tokens via `max_output_tokens`

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
                            │  └─ Serendipity, hindsight, temporal patterns
                            │
Tier 4 (dispatch.py) ──── Anaphora ("Solvstrat" is the anchor)
                            │  └─ Proactive signal check for Solvstrat drafts
                            │  └─ [PAST] tagging on calendar events
                            │  └─ Source selection: is_schedule + is_action
                            │
                            ▼
                    Gemini generates response (600 token cap)
                            │
                            ▼
                    Append proactive signals → Telegram reply
```

Each tier is independent and gracefully degrades. A Redis outage only reduces cache speed, not correctness. A memory embedding failure doesn't block calendar queries. This resilience is by design — the system was built for a single user who needs reliability over perfection.
