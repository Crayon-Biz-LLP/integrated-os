# Rhodey OS — System Specification
> Use this document with `/speckit.specify` when defining new features or changes.

---

## Current System State (as of June 2026)

### What is built and working
- Telegram webhook intake (`core/webhook/handler.py`) — classification, task/note routing, multimodal support
- Email ingestion — Gmail + Outlook → Supabase (`email_ingest.yml` GitHub Action)
- Email draft generation and approval via `ed` commands
- Pulse briefing — triggered via GitHub Actions, sends daily SITREP to Telegram
- Sentinel Watcher — 5-minute cron checks Google Calendar to send JIT AI-driven pre-flight briefs to Telegram
- Conversational Task Intake — disambiguation gate and Quick Pulse `CLARIFY` loops via Telegram (with conversation history support)
- Brain interrogation — hybrid Graph + Vector search (`interrogate_brain()`)
- Knowledge graph with 5 node types (person, organization, project, place, animal) and 16 edge types — all edges flow through HITL approval
- Pending graph edges/ nodes with inline editing UI in Decisions dashboard module
- Commitment tracking on tasks (direction, committed_to, committed_on)
- Sentiment extraction on memories (sentiment_score, sentiment, entities_mentioned)
- People ↔ graph_nodes linkage via graph_node_id FK
- Gmail + Outlook send via `senddraftreply()`
- `JOURNALSYNC` signal handler — triggers GitHub Actions from Google Sheets
- Personal capture pipeline — natural speech NOTE routing, `/note` command with entity extraction, `/api/roundup` evening check-in, voice memo→note pipeline
- RLS on sensitive tables (pending_graph_edges, pending_graph_nodes, messages, system_audit_logs, dead_letter_queue)
- **LLM Layer fully consolidated**: All API clients (Supabase, Gemini, Google) created once from canonical modules. Multi-key Gemini failover (3 keys). Unified fallback chain. Single rate limiter. Shared pending decision handler for call/whatsapp/teams channels.
- **Associative retrieval engine fully deployed**: 7-signal ranking (semantic, PPR, recency, importance, project, specificity, person_boost) replaces legacy pgvector-only `match_memories_hybrid`. 7 dedicated retrieval tables (passages, phrase_nodes, node_stats, passage_phrase_links, memory_bundle_links, alias_edges, index_runs). 470 memories indexed, 633 passages, 1305 phrase nodes, 3760 alias edges. Cold path 3.5–5.0s, warm path 1.8–3.5s. 4 per-site feature flags all ON in production. Forward indexing live for all new memories via `schedule_index_memory()`. Redis caching (1h LLM, 24h embeddings) with multi-key failover on 429 errors.
- **Brain Graph (Danny-centered)**: Split-pane graph page replaces legacy FullGraph. Left: Episode Stream (clustered memories grouped by entity/source/time, not raw fragments). Right: NeuralDisc (PixiJS v8 WebGL force-directed graph). Danny loads as permanent root. Node/background click, zoom/pan, collapsible sidebar. Backend: `/api/graph/ego`, `/api/graph/neighborhood`, `/api/graph/resolve-memory`, `/api/episodes/stream`. Infinite loop in PIXI scene rebuild fixed via ref-based callbacks and reduced dep array.

### What is broken or incomplete
- **MISSING**: No Decisions table (P3) — decisions are implicit in tasks/briefings
- **MISSING**: No graph edge expiry (P4) — edges older than 6 months may be stale
- **MISSING**: People table enrichment (P5) — org, last_interaction_date, notes columns not yet populated

---

## Active Feature Specifications

---

### SPEC-001: Atomic raw_dumps Pipeline

**What**: Separate the capture step from the embedding/memory step. Capture must always succeed. Embedding may fail gracefully.

**Why**: Currently, if `get_embedding()` throws, the record is still marked `completed`. The memory entry is never created. The data is silently lost.

**Acceptance Criteria**:
- `raw_dumps` records insert with `status: staged`
- A background processor (Pulse or a dedicated job) picks up `staged` records and attempts embedding
- On embedding success: insert into `memories`, mark `raw_dumps` as `processed`
- On embedding failure after 3 retries: insert into `dead_letter_queue`, mark `raw_dumps` as `embedding_failed`
- `completed` status is RETIRED — replaced by `processed` and `embedding_failed`
- No record is ever left in `staged` for more than 60 minutes without alerting Danny

**Out of scope**: Changing the classification logic, changing the Telegram receipt messages

---

### SPEC-002: system_audit_logs

**What**: Replace all `print(f"...")` error logging with structured writes to a Supabase table.

**Why**: Errors currently disappear into GitHub Actions log files that expire. There is no persistent record of what failed, when, or why.

**Acceptance Criteria**:
- New table: `system_audit_logs(id, function_name, event_type, message, raw_input, created_at)`
- `event_type` is one of: `error`, `warning`, `info`, `retry`, `dlq_write`
- All `except` blocks in `core/webhook/handler.py` and `core/pulse/engine.py` call `log_audit()` before any other action
- `log_audit()` itself must never throw — it wraps its own DB call in a try/except that falls back to `print()`
- `system_audit_logs` is never modified or deleted — append-only

**Out of scope**: UI for viewing audit logs (Streamlit dashboard is a separate spec)

---

### SPEC-003: dead_letter_queue

**What**: A dedicated table for records that have failed processing after the maximum retry count.

**Why**: Right now, failed records are silently dropped or left in an ambiguous state. The DLQ makes failures visible and recoverable.

**Acceptance Criteria**:
- New table: `dead_letter_queue(id, source_table, source_id, content, failure_reason, retry_count, resolved, created_at)`
- `source_table` is always `raw_dumps` for now
- After 3 failed embedding attempts, the record is inserted into `dead_letter_queue` and `raw_dumps.status` is set to `embedding_failed`
- Danny can resolve a DLQ record by sending `/dlq resolve <id>` in Telegram
- Resolving re-queues the record to `staged` for retry

**Out of scope**: DLQ for classification failures (future spec)

---

### SPEC-004: Janitor Heartbeat

**What**: A scheduled cron job that monitors pipeline health and alerts Danny via Telegram if records are stalling.

**Why**: Currently, Danny only discovers pipeline failures by manually running SQL queries.

**Acceptance Criteria**:
- GitHub Actions cron: every 30 minutes during 9am–10pm IST
- Checks: `SELECT COUNT(*) FROM raw_dumps WHERE status = 'staged' AND created_at < NOW() - INTERVAL '60 minutes'`
- If count > 0: sends Telegram alert: "⚠️ Pipeline Alert: {n} records stalled for 60+ mins."
- Also checks: `SELECT COUNT(*) FROM dead_letter_queue WHERE resolved = FALSE`
- If count > 0: sends daily (not every 30 mins) Telegram summary of unresolved DLQ items
- Janitor does NOT attempt to fix records — it only reports

**Out of scope**: Auto-remediation (future spec)

---

### SPEC-005: Backfill — Recover 41 Orphaned Notes

**What**: A one-time migration script to recover the 41 `raw_dumps` records that are marked `completed` but have no corresponding `memories` entry.

**Why**: Two weeks of Danny's strategic notes, milestones, and project context are missing from the memory system.

**Acceptance Criteria**:
- Script identifies all `raw_dumps` WHERE `status = 'completed'` AND content does not exist in `memories`
- Filters out noise: `content NOT ILIKE '%remind me%'` AND `LENGTH(content) > 20` AND `content != 'Testing the system'`
- For each qualifying record: attempts `get_embedding(content)` and inserts into `memories`
- On success: logs to `system_audit_logs` with `event_type: info`, `function_name: backfill`
- On failure: inserts into `dead_letter_queue`
- Script is idempotent — safe to run twice

**Out of scope**: Re-classifying intent or entity on these records

---

### SPEC-006: Graph Integrity — Guards + Human-in-the-Loop (OVERHAULED June 12)

**What**: Four-layer defence against bad graph data, plus HITL for ALL pending edges and high-risk nodes.

**Why**: The original spec (Guard A/B/HITL) was insufficient — 699 junk nodes (concept, emotional_state, resource) accumulated via auto-create. The ontology has been rebuilt from scratch. Key problems fixed:
- `raw_dumps` excluded from graph extraction (100% hallucinated edges)
- Catch-all relationship types removed (RELATES_TO, BELONGS_TO, AUTHORED, FEELS, INVOLVES)
- No more concept/emotional_state auto-creation during edge approval
- Emotions moved to memory metadata (sentiment fields), not graph
- Edge approval flow also added: all edges go through `pending_graph_edges` table with inline editing UI

**Acceptance Criteria**:

**Guard A — Orphaned Edge Cleanup (unchanged):**
- Both `core/pulse/graph.py:write_graph_edges_for_task` and `core/skills/backfill_graph.py` delete any edge with matching `metadata->>task_id` before inserting a new one
- No task can have more than one project edge at any time

**Guard B — Text-Anchored Hallucination Prevention (updated):**
- `extract_graph_elements()` prompt includes: "Only extract entities explicitly, verbatim stated in the text"
- After LLM extraction, Python validates each label: `label.lower()` must be a substring of `text.lower()`
- Hallucinated nodes + their edges dropped with audit warning
- "Danny" NOT automatically permitted — AUTHORED edge type was removed from the ontology

**HITL — Pending Approval for ALL Edges + Nodes (expanded):**
- Two staging tables: `pending_graph_nodes` + `pending_graph_edges`
- `pending_graph_nodes`: person/org/project nodes require HITL approval via Telegram `g{id}` flow
- `pending_graph_edges`: ALL extracted edges go through pending approval with inline editing UI
- Decisions UI (`/dashboard/decisions`) shows Graph Edges tab with Approve/Edit/Reject
- `_resolve_node()` in `graph.py` returns None instead of auto-creating `concept` nodes for missing labels
- Both tables have RLS enabled

**Guard D — Label-Drift Dedup (unchanged, extended to edges):**
- `fetch_pending_entities()` loads labels across ALL statuses
- Before insert: `ILIKE` exact + `ILIKE %label%` fuzzy fallback (≥6 chars)
- Unique index on `lower(trim(label))` prevents re-insertion
- `pending_graph_edges` deduped via normalised ILIKE matching with status-awareness

**Out of scope**: Graph edge expiry (P4 — deferred), decisions table (P3 — deferred)

