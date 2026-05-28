# 12. The Pulse Engine — Overview

The Pulse Engine (`core/pulse/engine.py`, 1,865 lines) is the crown jewel of Integrated-OS. It is a fully autonomous decision-making and intelligence cycle that runs on a scheduled basis (5x weekdays + 2x weekends) via GitHub Actions.

## The Full Briefing Cycle

Every Pulse run executes this complete pipeline:

### Phase 1: Pre-Flight Checks

1. **Idempotency Check**: Verifies `request_id` hasn't been processed before
2. **Zombie Recovery**: Resets dumps stuck in 'processing' >10 min back to 'pending'
3. **Auth Validation**: Confirms PULSE_SECRET
4. **Google→Supabase Sync**: Pulls completed tasks from Google Tasks, writes outcome memories
5. **Heartbeat Update**: Records `pulse_last_success` in `core_config`
6. **Pipeline Health Check**: Diagnostics on stuck dumps, failed queue, stale heartbeat
7. **Conversation History**: Fetches recent Telegram chat context

### Phase 2: Data Collection

8. **Fetch Projects** from graph_nodes (type='project') + projects table
9. **Fetch People** from people table
10. **Fetch Missions** from missions table
11. **Fetch Resources** (unenriched → batch AI enrichment)
12. **Claim Dumps**: Locks pending/staged/synced/partially_synced dumps with status='processing'. `awaiting_completion_match` dumps are **excluded** — they are owned exclusively by the completion handler (`core/webhook/completion_handler.py`) and should never flow into the briefing pipeline.
13. **Fetch Active Tasks** with project context

### Phase 3: Staging Area Sorter

A Gemini pre-processor categorizes ALL raw dumps into four buckets:

| Category | Action |
|----------|--------|
| NOTE | Generate embedding, save to memories (source='pulse_note'), mark dump completed |
| NOISE | Mark dump completed, silent discard |
| TASK | Keep in processing queue for main AI |
| COMPLETION | Keep in queue, also track for sealing |

This is a critical decoupling — it prevents noise and notes from polluting the main briefing AI while still persisting them.

### Phase 4: Context Building

The engine assembles a rich context block for the briefing AI:

- **Calendar Context**: Today's events, conflicts detected
- **Hindsight Memories**: Multi-signal vector search across all memory types
- **Graph Context**: Entity relationships, dependency chains, communication patterns
- **Active Tasks**: With project names, priorities, staleness flags
- **Overdue Items**: Past-due tasks sorted by urgency
- **Recent Completions**: Tasks done since last pulse
- **People**: Strategic weight distribution
- **Missions**: Active mission names for resource matching
- **Resources**: Recently enriched or unlinked resources
- **Season Context**: Current north star with expiry check
- **Conversation History**: Last several Telegram exchanges

### Phase 5: AI Generation

The heartbeat moment. A ~250-line system prompt with 30+ hard constraints is sent to Gemini 3 Flash. The AI must return structured JSON conforming to `PulseOutput`:

```python
class PulseOutput(BaseModel):
    completed_task_ids: List[CompletedTask]
    new_projects: List[NewProject]
    new_people: List[NewPerson]
    new_tasks: List[NewTask]
    resources: List[ResourceItem]
    logs: List[LogEntry]
    new_missions: List[str]
    briefing: str  # The actual Telegram message
```

### Phase 6: Write Phase

The AI's JSON output is parsed and executed:

A. **Completed Tasks**: Versioned update, Google Calendar delete, Google Tasks sync, outcome memory
B. **New Projects**: Dedup → insert → graph node (create/upgrade)
C. **New People**: Blocklist → dedup → insert
D. **New Tasks**: Batch insert → graph edges → Google Calendar → Google Tasks
E. **New Missions**: Dedup → insert → historical resource backfill
F. **Resources**: AI-generated bookmarks from URLs in Telegram messages, persisted with URL dedup
G. **Resource Enrichment**: Batch scrape + AI classification (strategic_note, category, embedding) via direct UPDATE; mission assignment at ≥0.80 confidence

### Phase 7: Speak Phase

The briefing string is post-processed:
- Normalize line breaks (Architect's Final Repair)
- Append rhythms section (weekends only)
- Sent to Telegram

## Decision Pulse (Standalone, No AI)

Email/call/WhatsApp pending decisions are **not** included in the main AI briefing. Instead, they're handled by a separate **Decision Pulse** (`process_decision_pulse()`), which runs on every cron trigger alongside the main briefing:

- **No AI call** — just fetches pending items, formats with interactive shortcodes, sends to Telegram
- **Auto-expires** items older than 7 days
- **Rotating Rhodey opener** — hardcoded templates, no AI generation
- **~2 second runtime** vs the main briefing's 15-30 seconds

Message format sent to Telegram:
```
📨 EMAIL DECISIONS (2) — reply [e{id}] yes/drop
[e42] Review Qhord contract (Solvstrat)
[e43] Follow up on invoice (CRAYON)

📞 CALL EXTRACTS (1) — reply [c{id}] yes/drop
📋 [c17] Call John re pilot (Solvstrat)

💬 WHATSAPP EXTRACTS (1) — reply [w{id}] yes/drop
💬 [w8] Check inventory (PERSONAL) — Mom
```

Shortcode replies (`e42 yes`, `c17 drop`, `w8 yes`) are processed by the same webhook handlers — no changes needed there.

## Key Design Principles

**Idempotency first**: Every run has a `request_id`. If the same run is triggered twice (e.g., overlapping cron + manual dispatch), the second invocation skips duplicate dumps.

**Graceful degradation**: Every AI call has fallback defaults. If JSON parsing fails, `ai_data` is initialized with empty arrays. The briefing falls back to raw text if Gemini fails.

**Async where possible**: Graph edge creation, Google sync, and resource enrichment all run as background tasks via `asyncio.create_task()`. The main flow is never blocked.

**No hallucination**: The prompt strictly forbids creating tasks from URLs unless explicitly commanded. Completed tasks must match explicitly. The briefing cannot list tasks that aren't in the SYSTEM_TASKS list.
