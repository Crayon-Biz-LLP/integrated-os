# 12. The Pulse Engine — Overview

> **⚠️ LEGACY WARNING**: This file has been updated to describe the **current** Briefing Engine architecture (as restructured in Part 58). For the full historical evolution from the original multi-agent loop to the current single-LLM-call architecture, see `58-final-architecture-overhaul.md`.

The Pulse Engine (`core/pulse/briefing.py`, split from the legacy `engine.py` in Part 57) is the autonomous intelligence cycle that generates Rhodey's briefings. It runs on a scheduled basis via GitHub Actions.

## Architecture Summary

The briefing engine is now a **single-LLM-call pipeline** with parallel pre-LLM data collection:

```
BriefingContext (dataclass) ← Parallel DB fetches (asyncio.gather)
  ↓
build_briefing_prompt(context) → Single Gemini SYNTHESIS_MODEL call
  ↓
Post-processing → Send to Telegram
```

### Key Changes from Legacy Architecture (Pre-Part 57)

| Legacy (engine.py) | Current (briefing.py) |
|---|---|
| 5 parallel AI agents + ToolRegistry multi-turn agent loop | Single LLM call with rich BriefingContext |
| 250-line monolithic prompt | Structured prompt assembled from typed context fields |
| Staging Area Sorter (Gemini pre-processor) | Removed — all dumps flow through standard processing pipeline |
| ToolRegistry with function calling | Action Planner handles structured operations |
| Context Hydration Engine (SimpleCache) | Context Registry (`core/context/`) with strategy-based hydration |
| `engine.py` ~1,492 lines | `briefing.py` + `decision_pulse.py` + `sentinel.py` split |

## The Full Briefing Cycle

Every Pulse run executes this complete pipeline:

### Phase 1: Pre-Flight Checks

1. **Idempotency Check**: Verifies `request_id` hasn't been processed before
2. **Zombie Recovery**: Resets dumps stuck in 'processing' >10 min back to 'pending'
3. **Auth Validation**: Confirms PULSE_SECRET
4. **Google→Supabase Sync**: Pulls completed tasks from Google Tasks, writes outcome memories. **Skips recurring tasks** (those with a `recurrence` column value) — they go through auto-expiry instead.
5. **Recurring Task Auto-Expiry**: `_auto_expire_recurring_tasks()` parses RRULE `UNTIL` dates and marks expired recurring tasks as `cancelled` via versioned update. Cache invalidation runs after auto-expiry update to prevent stale data.
6. **Heartbeat Update**: Records `pulse_last_success` in `core_config`
7. **Pipeline Health Check**: Diagnostics on stuck dumps, failed queue, stale heartbeat
8. **Conversation History**: Fetches recent Telegram chat context

### Phase 2: Data Collection (BriefingContext)

All data is collected **upfront** into a `BriefingContext` dataclass via parallel `asyncio.gather()` calls. This phase reads from:

- **Tasks**: Active tasks with project context, overdue tasks, urgency lists
- **Projects & People**: Graph-synced entities
- **Resources**: Recent URLs (pattern context, newly enriched, recent URLs)
- **Memories**: Hindsight memories (On this day, temporal patterns)
- **Calendar**: Today's events from Google Calendar
- **Season Config**: Current strategic context
- **Serendipity**: Cross-domain connection paths via recursive CTE (`find_serendipity_paths`)
- **Graph Centrality**: Top-connected entities
- **Pulse Context**: Previous briefing summary, conversation history

### Phase 3: Prompt Assembly & LLM Call

The `build_briefing_prompt()` function constructs a structured prompt from all `BriefingContext` fields and sends it as a single call to `gemini-3.5-flash` (SYNTHESIS_MODEL). No agent loop, no tool calling during the LLM turn — the LLM generates one complete briefing response.

**Prompt sections:**
- System persona (mode-specific: MORNING / AFTERNOON / CLOSING LOOP / WEEKEND REST)
- Mode overrides (URGENT / NIGHT)
- Calendar and task data
- Hindsight memories and serendipity connections
- Strategic context and season goals
- Recipients section with people and practices

### Phase 4: Post-Processing

The LLM response is post-processed:
- Normalize line breaks
- Strip any remaining formatting inconsistencies
- Append section headers
- Sent to Telegram

### Phase 5: Background Cleanup

After the main response is sent:

- **Cache invalidation**: Stale task caches are invalidated after auto-expiry
- **Pulse context update**: Briefing summary saved for next pulse's continuity
- **Graph backfill**: Run via CI after-deployment steps

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

Each item gets an inline keyboard row with ✅ approve / ❌ reject buttons. Callback data format: `approve_e42`, `reject_g1`, etc.

## Sentinel Watcher (High-Frequency JIT Briefs)

Running parallel to the scheduled Pulses is the **Sentinel Watcher** (`core/pulse/sentinel.py`), which executes every 5 minutes via cron-job.org → `POST /api/sentinel`.

- **Function**: Scans Google Calendar for events starting within 0–45 minutes from `now`.
- **Pre-Flight Briefing**: Runs `execute_context_strategy(query=event_title, strategy=PRE_FLIGHT_CONFIG)` — entity-anchored context retrieval against tasks, people, emails, and meeting minutes. Queries Gemini (SYNTHESIS_MODEL, temperature=0.2) to summarize the verified context with explicit date/inference awareness.
- **Delivery**: Sends a high-priority Telegram message. Push notification sent when meeting is ≤15 min away.
- **Deduplication**: Logs `Sentinel_Sent:{event_id}` to `audit_logs` so a single meeting never triggers duplicate alerts across the 5-minute polling windows.
- **Piggyback jobs**: Post-meeting capture prompts (5–30 min after event ends), weekly stale-task sweep (Sunday), unanswered clarification dispatch, delegation alerts (waiting_on >3d), orphan recurring calendar cleanup, graph integrity sweep.

## Key Design Principles

**Idempotency first**: Every run has a `request_id`. If the same run is triggered twice, the second invocation skips duplicate processing.

**Graceful degradation**: Every AI call has fallback defaults. The briefing falls back to raw context string if Gemini fails.

**Parallel data collection**: All DB reads in Phase 2 run concurrently via `asyncio.gather()`, reducing wall-clock time from ~15s sequential to ~4-6s parallel.

**Streaming responses**: The briefing response is streamed via Telegram's `sendMessage` with `parse_mode='Markdown'`. The LLM response is captured in full before sending.

**No hallucination**: The prompt strictly forbids creating tasks from URLs unless explicitly commanded. Completed tasks must match explicitly. CONTEXT_SECTION_RULES annotation prevents the LLM from confusing historical sections with active tasks.
