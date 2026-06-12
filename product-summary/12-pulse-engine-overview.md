# 12. The Pulse Engine — Overview

The Pulse Engine (`core/pulse/engine.py`, ~1,492 lines) is the crown jewel of Integrated-OS. It is a fully autonomous decision-making and intelligence cycle that runs on a scheduled basis (5x weekdays + 2x weekends) via GitHub Actions.

## The Full Briefing Cycle

Every Pulse run executes this complete pipeline:

### Phase 1: Pre-Flight Checks

1. **Idempotency Check**: Verifies `request_id` hasn't been processed before
2. **Zombie Recovery**: Resets dumps stuck in 'processing' >10 min back to 'pending'
3. **Auth Validation**: Confirms PULSE_SECRET
4. **Google→Supabase Sync**: Pulls completed tasks from Google Tasks, writes outcome memories. **Skips recurring tasks** (those with a `recurrence` column value) — they go through auto-expiry instead.
5. **Recurring Task Auto-Expiry**: `_auto_expire_recurring_tasks()` parses RRULE `UNTIL` dates and marks expired recurring tasks as `cancelled` via versioned update.
6. **Heartbeat Update**: Records `pulse_last_success` in `core_config`
7. **Pipeline Health Check**: Diagnostics on stuck dumps, failed queue, stale heartbeat
8. **Conversation History**: Fetches recent Telegram chat context

### Phase 2: Data Collection

9. **Fetch Projects** from graph_nodes (type='project') + projects table
10. **Fetch People** from people table
11. **Fetch Resources** (unenriched → batch AI enrichment; URLs from graph_nodes deduped in-memory)
12. **Fetch `core_config`** (cached in-memory across phases for `last_pulse_summary`)
13. **Claim Dumps**: Locks pending/staged/synced/partially_synced dumps with status='processing'. `awaiting_completion_match` dumps are **excluded** — they are owned exclusively by the completion handler (`core/webhook/completion_handler.py`) and should never flow into the briefing pipeline.
14. **Fetch Active Tasks** with project context

### Phase 3: Staging Area Sorter

A Gemini pre-processor categorizes ALL raw dumps into four buckets:

| Category | Action |
|----------|--------|
| NOTE | Generate embedding, save to memories (source='pulse_note'), mark dump completed |
| NOISE | Mark dump completed, silent discard |
| TASK | Keep in processing queue for main AI |
| COMPLETION | Keep in queue, also track for sealing |

This is a critical decoupling — it prevents noise and notes from polluting the main briefing AI while still persisting them.

### Phase 4: Context Building (Hydration Engine)

The engine utilizes the **Context Hydration Engine** (`ContextProvider` using `SimpleCache` backed by Upstash Redis for distributed persistence) to efficiently assemble a rich, token-optimized context block. It employs semantic selection and hard safeguards (e.g., always including overdue tasks while semantically sorting the rest).

- **Calendar Context**: Today's events, conflicts detected
- **Hybrid Cross-Referenced Context**: Fuses vector semantic memory search (with exponential time decay and importance weighting) and graph traversal into a single unified block, dynamically tagging memories with their graph relationships.
- **Graph Centrality**: Highlights the top 3 most connected hub entities across domains.
- **Session Memory**: Injects a 1-2 sentence summary of the *previous* briefing to maintain cross-pulse continuity.
- **Active Tasks**: With project names, priorities, staleness flags
- **People**: Strategic weight distribution
- **Conversation History**: Last several Telegram exchanges

### Phase 5: AI Generation & Tool Execution

The heartbeat moment. The engine runs a multi-turn agent loop (`run_agent_loop` via `core/pulse/agent.py`). A comprehensive system prompt is sent to Gemini 3.5 Flash, equipped with a `ToolRegistry` (defined in `core/pulse/tools.py`) of specific capabilities.

Before the AI loop, the **Adaptive Briefing Learner** (`memory.py`) refines prompt patterns by analyzing past briefing outcomes — but only on Sundays (`now.weekday() == 6`) to avoid unnecessary latency on weekdays. Instead of returning a monolithic JSON blob, the model issues tool calls to modify state sequentially.

Available Tools:
- `update_task_status` — **on recurring tasks, `done` skips the next instance** (write outcome memory, series continues); only `cancelled` ends the entire series
- `create_project`
- `create_person`
- `create_task` — accepts optional `recurrence` parameter (iCalendar RRULE string)
- `update_task`
- `create_resource`
- `create_cluster`
- `write_memory`
- `skip_recurring_instance` — deletes the next individual Google Calendar instance for a recurring task without affecting the series

The agent executes tools in a `while True` loop until it completes its logic and returns a final Markdown briefing payload (and optionally hits a `HitlInterrupt` if it requires user confirmation).

### Phase 6: Post-Processing

Each sub-step below is wrapped in an isolated try/except — a failure in batch enrichment or cluster discovery does not crash the entire pipeline. Remaining phases continue independently.

A. **Completed Tasks**: Versioned update, Google Calendar delete, Google Tasks sync, outcome memory
B. **New Projects**: Dedup → insert → graph node (create/upgrade)
C. **New People**: Blocklist → dedup → insert
D. **New Tasks**: Batch insert → graph edges → Google Calendar → Google Tasks
E. **New Clusters**: Dedup → insert → historical resource backfill
F. **Resources**: AI-generated bookmarks from URLs in Telegram messages, persisted with URL dedup
G. **Resource Enrichment**: Batch scrape + AI classification (strategic_note, category, embedding) via direct UPDATE; cluster assignment at ≥0.70 confidence

### Phase 7: Speak Phase

The briefing string is post-processed:
- Normalize line breaks (Architect's Final Repair)
- Append rhythms section (weekends only)
- Sent to Telegram

### Phase 8: Graph Backfill (CI Cycle)

After the briefing pipeline completes, the CI workflow runs a graph health pipeline (`run_backfill()` in `core/skills/backfill_graph.py`):

1. **`backfill_orphaned_tasks()`** — Re-links task nodes that lost their parent project edge
2. **`backfill_emotion_edges()`** — Creates `Danny → FEELS → {emotional_state}` edges for orphaned emotional concepts (e.g., Suicidal Ideation, Depression, Broken)
3. **`backfill_orphaned_node_edges()`** — Connects any non-task node lacking a direct Danny edge, assigning type-appropriate relationships (OWNS, INTERESTED_IN, KNOWS, WORKS_WITH, FEELS, PRACTICES)

This ensures the knowledge graph remains healthy (zero orphaned non-task nodes, all emotions connected) across extraction cycles. Post-backfix, the graph maintains ~1020 nodes and ~2350+ edges with zero non-task nodes lacking a direct Danny connection.

## Decision Pulse (Standalone, No AI)

Email/call/WhatsApp pending decisions are **not** included in the main AI briefing. Instead, they're handled by a separate **Decision Pulse** (`process_decision_pulse()`), which runs every 30 minutes via cron-job.org:

- **No AI call** — just fetches pending items, formats with interactive inline keyboards, sends to Telegram
- **Auto-expires** items older than 7 days
- **Rotating Rhodey opener** — hardcoded templates, no AI generation
- **~2 second runtime** vs the main briefing's 15-30 seconds
- **Subject fallback**: When `suggested_title` is NULL (LLM couldn't formulate a task), falls back to the email's `subject` field to avoid "Untitled" rows

Message format sent to Telegram with inline keyboard buttons:
```
📨 EMAIL DECISIONS (2) — tap to approve/drop
[e42] Review Qhord contract (Solvstrat)
[e43] Follow up on invoice (CRAYON)

📞 CALL EXTRACTS (1) — tap to approve/drop
📋 [c17] Call John re pilot (Solvstrat)

💬 WHATSAPP EXTRACTS (1) — tap to approve/drop
💬 [w8] Check inventory (PERSONAL) — Mom

🕸️ GRAPH NODES (3) — tap to approve/drop
👤 [g1] Solvstrat (organization)
👤 [g2] Paulson (person)
```

Each item gets an inline keyboard row with ✅ approve / ❌ reject buttons. Callback data format: `approve_e42`, `reject_g1`, etc. Text shortcode replies (`g1 yes`, `e42 approve`) also work via the same webhook handlers.

## Sentinel Watcher (High-Frequency JIT Briefs)

Running parallel to the scheduled Pulses is the **Sentinel Watcher** (`core/pulse/sentinel.py`), which executes every 5 minutes via `.github/workflows/sentinel.yml`.

- **Function**: Scans Google Calendar for events starting exactly 1-20 minutes from `now`.
- **Pre-Flight Briefing**: Fetches relevant active tasks matching the meeting title from Supabase, then queries Gemini to generate a fast, 2-bullet context summary (e.g. *"Meeting with John. Context: Last action was X"*).
- **Delivery**: Sends a high-priority Telegram message (acting as a secondary, smarter alarm).
- **Deduplication**: Logs the `event_id` to `audit_logs` so a single meeting never triggers duplicate alerts across the 5-minute polling windows.
- **Calendar Enforcer**: Works alongside a modification to `sync_to_calendar` that overrides Google Calendar defaults to enforce strict 1-hour and 15-minute native popup reminders.

## Key Design Principles

**Idempotency first**: Every run has a `request_id`. If the same run is triggered twice (e.g., overlapping cron + manual dispatch), the second invocation skips duplicate dumps.

**Graceful degradation**: Every AI call has fallback defaults. If JSON parsing fails, `ai_data` is initialized with empty arrays. The briefing falls back to raw text if Gemini fails.

**Async where possible**: Graph edge creation, Google sync, and resource enrichment all run as background tasks via `asyncio.create_task()`. The main flow is never blocked.

**No hallucination**: The prompt strictly forbids creating tasks from URLs unless explicitly commanded. Completed tasks must match explicitly. The briefing cannot list tasks that aren't in the SYSTEM_TASKS list.
