# Rhodey OS — System Specification
> Use this document with `/speckit.specify` when defining new features or changes.

---

## Current System State (as of May 2026)

### What is built and working
- Telegram webhook intake (`core/webhook/handler.py`) — classification, task/note routing, multimodal support
- Email ingestion — Gmail + Outlook → Supabase (`email_ingest.yml` GitHub Action)
- Email draft generation and approval via `ed` commands
- Pulse briefing — triggered via GitHub Actions, sends daily SITREP to Telegram
- Sentinel Watcher — 5-minute cron (`sentinel.yml`) checks Google Calendar to send JIT AI-driven pre-flight briefs to Telegram
- Conversational Task Intake — disambiguation gate and Quick Pulse `CLARIFY` loops via Telegram (with conversation history support)
- Brain interrogation — hybrid Graph + Vector search (`interrogate_brain()`)
- Graph nodes and edges — entity relationship tracking
- Gmail + Outlook send via `senddraftreply()`
- `JOURNALSYNC` signal handler — triggers GitHub Actions from Google Sheets
- Personal capture pipeline — natural speech NOTE routing, `/note` command with entity extraction, `/api/roundup` evening check-in, voice memo→note pipeline

### What is broken or incomplete
- **CRITICAL**: `raw_dumps` records are marked `completed` even when embedding fails → 41+ orphaned records with `embedding: null`
- **CRITICAL**: `handle_confident_note()` in `core/webhook/handler.py` runs embedding synchronously in the webhook response path — if Gemini is slow, the webhook times out
- **MISSING**: No `system_audit_logs` table — errors go to `print()` and disappear
- **MISSING**: No `dead_letter_queue` for failed embeddings
- **MISSING**: No Janitor/heartbeat monitoring the pipeline health
- **MISSING**: `raw_dumps` → `tasks` enrichment (project linking, priority assignment) in Pulse is not verified as complete
- **PARTIAL**: Temporal lineage (is_current pattern) not implemented on any table

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

### SPEC-006: Graph Integrity — Guards + Human-in-the-Loop

**What**: Three-layer defence against bad graph data and LLM hallucination in the knowledge graph.

**Why**: The graph is the backbone of Rhodey's intelligence. Two concrete bugs were found:
- Orphaned `BELONGS_TO` edges persisting after tasks changed projects (task 161 → Solvstrat ghost)
- LLM-extracted `WORKS_AT` edges hallucinating "Solvstrat" from a memory that never mentioned it

**Acceptance Criteria**:

**Guard A — Orphaned Edge Cleanup:**
- Both `core/pulse/graph.py:write_graph_edges_for_task` and `core/skills/backfill_graph.py` delete any `BELONGS_TO` edge with matching `metadata->>task_id` before inserting a new one
- No task can have more than one BELONGS_TO edge at any time

**Guard B — Hallucination Prevention:**
- `extract_graph_elements()` prompt includes: "Only extract entities explicitly, verbatim stated in the text"
- After LLM extraction, Python validates each label: `label.lower()` must be a substring of `text.lower()`
- Hallucinated nodes + their edges dropped with audit warning
- "Danny" always valid (for AUTHORED edges)

**HITL — Pending Approval + NLP Correction Loop:**
- New table: `pending_graph_nodes(id, label, type, source_text, proposed_edges, status, created_at)`
- `get_or_create_node()` and `upsert_nodes()` check `pending_entities_cache` before inserting
- Decision Pulse (`core/pulse/engine.py`) queries pending items and renders inline keyboard with `g{id}` prefix
- User can use quick inline commands (`g1 yes`, `g1 drop`) or reply with free-text (e.g., "g1 is actually an organization")
- Free-text is parsed by Gemini (`core/webhook/graph.py`), cached in an active session, and presented for explicit user confirmation (`yes`/`no`) before applying
- On approve → node upserted into `graph_nodes`
- On reject → status set to `rejected`
- In-memory cache (`pending_entities_cache`) prevents duplicate entries during batch runs

**Dedup Fix — Label-Drift Re-Insertion Prevention:**
- `fetch_pending_entities()` loads labels across ALL statuses (`pending`, `approved`, `rejected`), not only `pending`
- Before insert, `_check_pending_label_exists()` runs strict normalised `ILIKE` + fuzzy `ILIKE %label%` fallback (≥6 chars) against `pending_graph_nodes`
- Unique index `idx_pending_graph_nodes_label_dedup` on `lower(trim(label))` provides a hard DB-level constraint
- Together, these prevent approved/rejected labels from being re-submitted as new pending rows on subsequent backfill runs

**Out of scope**: Gating algorithmically-created edges (PRECEDES, FOLLOWED_BY) — low risk

