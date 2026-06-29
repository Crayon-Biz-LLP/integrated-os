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
- **Concept Fluidity (Synaptic Plasticity)**: Ontology supports `concept` nodes with `EVOKES`, `RELATES_TO`, `ASSOCIATED_WITH` edge types. Abstract concepts extracted from 416 historical memories via `concept_sweep_batch.py`. All concept nodes guarded by HITL approval with 85%+ similarity dedup and 1-click merge.
- **Clarifier Phase 2 (Live)**: Full disambiguation engine for graph nodes — 85%+ similarity triggers Telegram questions, 95%+ triggers auto-merge confirmation, edge contradiction detection, low-confidence (<0.7) edge verification, concept alias dedup.
- **Meeting Minutes Ingestion & Graph Resolution**: The classification layer (`classify.py`) natively supports structured meeting minutes (MoMs, PDFs) routing explicitly to `NOTE` even if action items are present. Real-time entity extraction intercepts explicit `organization` and `project` references, resolving them against the graph layer, and automatically binds the canonical UUIDs (`organization_id`, `project_id`) securely to the memory row.
- **Canonical Pages Migration**: Master pages are now synthesized at the **organization** level rather than the project level, acting as holistic domain umbrellas that aggregate context, active tasks, and relationships across all sub-projects. The `canonical_pages` table is explicitly tethered via an indexed `organization_id` foreign key. Orgs failing the minimum fragment threshold skip synthesis gracefully to prevent data loss.
- **Pipeline Integrity**: Raw dumps state machine (`staged` → `processed`/`embedding_failed`), `system_audit_logs` table, `dead_letter_queue` table, `log_audit()`/`write_dlq()` utilities, idempotency guard on raw_dumps insert, `is_recent_raw_dump()` check.
- **Temporal Lineage**: PostgreSQL BEFORE UPDATE triggers on `tasks` and `canonical_pages` tables preserve history without breaking primary keys. Old rows archived as `is_current=false` with incremented version. Memories table also has `is_current`/`version`/`supersedes_id` columns (now correctly typed `int8`).
- **Frontend Pages**: Fully built Calendar (Day/Week/Month/Agenda views with Google+Outlook sources), Messages (Telegram-style chat interface with auto-scroll, metadata parsing), and Graph (split-pane NeuralDisc + Episode Stream).
- **Conversational State Engine**: Persistent `conversation_threads` + `conversation_workflows` tables. Thread routing chain (open workflow → exact entity → prior bot question → fallback). Workflow state with deterministic phrase matcher (confirm/decline via set-based matching bypassing LLM), LLM fallback for ambiguous replies, unrelated note preservation, atomic idempotency via `.eq('status', 'active')`, 24h expiry pruning. Query carry-forward persists structured `active_anchor` (`{id, name, type, last_action, last_task_id, last_project_id, last_org_id, last_summary_snippet, last_mentioned_at}`) to threads for cross-turn anaphora resolution. Thread summarization on overflow (5000 token budget, extractive compression stored on `conversation_threads.summary`, loaded into anaphora prompt alongside anchor context).
- **Memory Hygiene**: Expiry enforcement in associative retrieval (post-PPR filter). Application-level versioning via `version_memory_for_update()`. Deletion/index cleanup via `cleanup_memory_retrieval_index()` + daily orphan sweep. Raw dump lifecycle cleanup (stale records auto-abandoned after 24h via Sentinel).
- **Memory Versioning**: `version_memory_for_update()` helper in `core/services/db.py` archiving memories before mutation. Wired into entity enrichment and degraded completion paths.
- **Memory Expiry Sweep (M5)**: Sentinel piggyback sweeps `memories WHERE expires_at < now` every 12h, cascades cleanup through retrieval index tables via `cleanup_memory_retrieval_index()`, then runs orphan sweep.
- **Orphan Calendar Cleanup (T4)**: Sentinel piggyback finds cancelled recurring tasks with lingering `google_event_id`, deletes the Google Calendar event series, and nulls the ID to prevent ghost re-creation.

### What is broken or incomplete
- **MISSING**: No Decisions table (P3) — decisions are implicit in tasks/briefings [COMPLETED]
- **MISSING**: No graph edge expiry (P4) — edges older than 6 months may be stale [COMPLETED]
- **MISSING**: People table enrichment (P5) — org, last_interaction_date, notes columns not yet populated [COMPLETED]
- **DEFERRED**: Graph UI polish — PIXI object pooling, smooth zoom/pan animations, multi-select + expand-in-place nodes, episode stream infinite scroll + date range [STILL DEFERRED]
- **DEFERRED**: TF-002 Graph Edge Expiry — last_confirmed_at/valid_until — edges older than 90 days auto-expired via sentinel [COMPLETED]
- **DEFERRED**: TF-003 People Table Enrichment — organization_name, last_interaction_date from graph edges [COMPLETED]

### Rhodey Audit — Good-to-Have (Future Backlog)

| # | Item | Effort | Type | Notes |
|---|------|--------|------|-------|
| X1 | Increase `recent_tasks` cache TTL 60→300s | ~5m | Optimization | Already deployed in this session |
| K3 | Proactive thread resumption — "Anything update on X?" nudge for threads idle >3d | ~1h | Feature | Risk of over-nudging; sentinel piggyback |
| K4 | Workflow expiry nudge — "Still working on this?" before 24h workflow expiry | ~1h | Feature | sentinel piggyback + audit_log gate |
| B3 | Briefing personalization — feed user reactions into Sunday learner | ~3h | Enhancement | Needs `briefing_feedback` table |
| B1 | Briefing prompt compression — holistic token budget allocator | ~2h | Optimization | Gemini context window easily absorbs current size |
| S3 | Energy-aware scheduling — task complexity classification + calendar density analysis | ~4h | New capability | Privacy-adjacent; needs explicit opt-in design |

---

## Active Feature Specifications

---

### SPEC-001: Atomic raw_dumps Pipeline [COMPLETED]

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

### SPEC-002: system_audit_logs [COMPLETED]

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

### SPEC-003: dead_letter_queue [COMPLETED]

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

### SPEC-004: Janitor Heartbeat [COMPLETED]

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

### SPEC-005: Backfill — Recover 41 Orphaned Notes [COMPLETED]

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

### SPEC-006: Graph Integrity — Guards + Human-in-the-Loop + Concept Fluidity [COMPLETED]

**What**: Four-layer defence against bad graph data, plus HITL for ALL pending edges and high-risk nodes. Concept nodes re-introduced via Synaptic Plasticity upgrade.

**Why**: The original spec (Guard A/B/HITL) was insufficient — 699 junk nodes (concept, emotional_state, resource) accumulated via auto-create. The ontology has been rebuilt from scratch. Key problems fixed:
- `raw_dumps` excluded from graph extraction (100% hallucinated edges)
- Catch-all relationship types removed (RELATES_TO, BELONGS_TO, AUTHORED, FEELS, INVOLVES)
- Concept nodes re-introduced under strict HITL control via Concept Fluidity upgrade (T-401)
- No concept auto-creation during edge approval — all go through pending tables with 85%+ dedup
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

**Concept Fluidity (Synaptic Plasticity) [ADDED]:**
- Ontology supports `concept` nodes with `EVOKES`, `RELATES_TO`, `ASSOCIATED_WITH` edge types
- `concept_sweep_batch.py` extracts abstract concepts from historical memories
- All concept nodes pass through the same HITL flow: pending table → approval via `g{id}` shortcode
- Deduped via 85%+ similarity detection with 1-click merge confirmation
- No concept auto-creation: all go through `pending_graph_nodes` with explicit approval

**Guard D — Label-Drift Dedup (unchanged, extended to edges):**
- `fetch_pending_entities()` loads labels across ALL statuses
- Before insert: `ILIKE` exact + `ILIKE %label%` fuzzy fallback (≥6 chars)
- Unique index on `lower(trim(label))` prevents re-insertion
- `pending_graph_edges` deduped via normalised ILIKE matching with status-awareness

**Out of scope**: Graph edge expiry (P4 — deferred), decisions table (P3 — deferred)

---

### SPEC-009: Simulation Tests — Rhodey Autonomous Behaviours [IN PROGRESS]

**What**: 5-suite simulation test plan to validate T1, S5, M5, T4, K2, C3, X3 autonomous behaviours with quantified assertions before committing the 14-file uncommitted batch (626 insertions, 80 deletions).

**Fallback contracts**: All degraded-mode behaviour is codified in `core/FALLBACK_CONTRACTS.md`. Tests assert against those contracts, not against implementation internals.

**Suites**:

| Suite | Focus | Behaviours | Key Assertions |
|-------|-------|------------|----------------|
| 1 | Positive path + call order | T1, S5, T4, M5 | Exact DB mutation order per sentinel piggyback |
| 2 | Cognitive routing | K2, C3, X3 | K2: entity matching prioritisation, silent fallback. C3: SAFE_HOLD output + vaulted-as-NOTE. X3: embedding boost applied to semantically related tasks |
| 3 | Boundary / no-op | All 7 | Empty inputs, unknown entities, no-op conditions (nothing to escalate, no expired memories, no orphan events) |
| 4 | Idempotency | T1, S5, T4, M5 | First run: state mutated. Second run: NO new state mutations (no `tasks.update`, no `memories.delete`, no new audit_log write beyond harmless reads). Allow: audit_log SELECT queries |
| 5 | Failure paths (fail-closed/open/retry) | All 7 | Explicit outcome per path: fail-closed (task creation blocked), fail-open (graceful degradation with audit WARNING), retry-then-fail (M5 per-item cleanup). Each WARNING/ERROR log must carry the same `trace_id` as the triggering event — use `set_trace_id("test-...")` |

**Idempotency nuance (Suite 4)**:
- Assert "no state mutations" not "absolutely nothing"
- Second run may produce harmless `SELECT` queries on `audit_logs` (the idempotency gate itself)
- Deny: new `tasks.update()`, new `memories.delete()`, new `tasks.insert()`, new `calendar.events.delete()`
- Deny: new `audit_logs` INFO writes for actual work done

**trace_id assertion (Suite 5)**:
- Set known `trace_id` via `set_trace_id("test-<scenario>")` before triggering each failure
- After failure: query `audit_logs WHERE metadata->>'trace_id' = 'test-<scenario>' AND level IN ('WARNING','ERROR')`
- Assert ≥ 1 matching row

**Teardown (shared fixture)**:
1. No `[SIM_TEST]` rows remain in any table (memories, tasks, raw_dumps, graph_nodes, audit_logs)
2. No orphan retrieval/index rows for the sandbox namespace (retrieval_passages, retrieval_phrase_nodes with no corresponding memories.id)
3. Run `sweep_orphan_retrieval_entries()` after each suite teardown to verify clean state

**Execution**: All suites run with `SANDBOX_DB=true`, mocked Google APIs (Calendar, Tasks), mocked Telegram, mocked LLM. Existing 36-test regression suite must continue to pass.

