> **⚠️ LEGACY WARNING**: This file references architecture from an earlier phase. Some modules mentioned (e.g., handle_confident_task, process_single_dump, quick_process, janitor) have been replaced or removed in Parts 57-61. The core concept remains valid — see 58-final-architecture-overhaul.md for current architecture.
# 6. Telegram Intake & Webhook

## Overview

The Telegram webhook is the primary real-time input channel. A FastAPI POST endpoint at `/api/webhook` receives Telegram updates and processes them through a 6-stage pipeline.

## The 6-Stage Pipeline

### Stage 1: Deduplication

Telegram can redeliver updates if it doesn't receive a 200 response quickly enough. The system stores every `update_id` in the `processed_updates` table with a UNIQUE constraint. If PostgreSQL returns a 23505 unique violation, the update is silently discarded.

```python
supabase.table('processed_updates').insert({
    "update_id": update_id,
    "chat_id": chat_id,
    "metadata": {"source": "telegram"}
})
```

A cleanup job purges entries older than 72 hours.

### Stage 2: Authorization

The handler checks that `message.chat.id` matches the configured `TELEGRAM_CHAT_ID` environment variable. Any message from an unknown chat is silently dropped — no logging, no error.

### Stage 3: Multimodal Dispatch

If the message contains non-text content (photos, voice messages, audio files, documents), it's routed to `process_multimodal_content()` in `multimodal.py` rather than the standard text pipeline:

- **Photos** → sent to Gemini as base64 image data for OCR/extraction
- **Voice/Audio** → sent to Gemini for transcription and task extraction
- **Documents (PDF, DOCX, text)** → sent to Gemini for content parsing
- All multimodal results return structured JSON with tasks, notes, or delegate requests

### Stage 4: Inline Keyboard Decision Resolution

Before the standard classification path, the system checks if the incoming update is a `callback_query` from an Inline Keyboard (which replaced the old text-based shortcode system). This is the fastest, one-tap interaction path.

The user taps "✅ e123" or "❌ e123" directly in Telegram:
- **Email decisions**: `approve_e123` / `reject_e123`
- **Call decisions**: `approve_c456` / `reject_c456`
- **WhatsApp decisions**: `approve_w789` / `reject_w789`

The webhook's `process_callback_query` intercepts it, triggers the corresponding resolution function, updates the original message UI, and answers the callback query to clear the loading state.

### Stage 5: Clarification Resolution

If a previous message triggered a clarification flow (task update vs. create, disambiguation), the system checks for single-letter replies:

| Letter | Action |
|--------|--------|
| `u` | Update existing task |
| `n` | Create new task (or flip to note) |
| `t` | Flip to task |
| `q` | Treat as query |
| `b` | Treat as brief |
| `r` | Treat as delegate |
| `p` | Treat as practice declaration |
| `x` | Discard / noise |

Each routes to a dedicated resolution function that looks up the stored clarification context from the conversation session.

### Stage 6: Intent Classification & Routing

If no shortcode or clarification match is found, the message goes to Gemini for intent classification (see next chapter). The classified intent determines routing:

| Intent | Handler | Action |
|--------|---------|--------|
| TASK | `handle_confident_task()` | Plan actions via `plan_actions()` → `execute_planned_actions()` (Includes Semantic Guard + Graph Edges) |
| NOTE | `handle_confident_note()` | Insert raw_dump (staged), embed, save to memories |
| COMPLETION | `handle_confident_completion()` | Embed → save to memories, LLM match to open task, close + Google sync, or park as `awaiting_completion_match` |
| QUERY | `interrogate_brain()` | Universal intelligence — hybrid graph + vector + canonical search, parallel 14-source fetch, anaphora resolution, active anchor scoping, source selection heuristics |
| DAILY_BRIEF | `handle_daily_brief()` | Daily overview — calendar + tasks + overdue for today/tomorrow only |
| DELEGATE | agent_queue insert | Research agent picks up via background worker |
| DECLARE_PRACTICE | `handle_declare_practice()` | Create practice graph node with full metadata |
| NOISE | `handle_noise()` | Send 👍 |
| CLARIFICATION_NEEDED | Clarification question | Ask user to disambiguate |

**Routing note (June 2026 update):** Schedule questions with date ranges (e.g. "Meetings this week?", "What's on my calendar tomorrow?") now route to `QUERY`/`interrogate_brain()` rather than `DAILY_BRIEF`. This is because `interrogate_brain` supports arbitrary date ranges via `resolve_dates_from_query()`, parallel context fetching, and source selection heuristics. `DAILY_BRIEF` is reserved for explicit daily overview requests ("good morning", "give me my day"). The classifier distinguishes these via prompt rules in `classify.py`.

### Completion Handler Pipeline (State Machine)

If the inline classifier emits `COMPLETION` (or if it matches an existing task via Semantic Guard), it triggers an inline versioned update and writes an `outcome_memory` to the Knowledge Graph. Note: The dedicated `handle_confident_completion` state machine below is used when explicitly categorized prior to inline processing. and enters a dedicated state machine in `core/webhook/completion_handler.py`. This prevents race conditions between the inline completion handler and the pulse's own task-processing pipeline.

#### State Machine

The dump transitions through these statuses:

```
processing_completion → awaiting_completion_match | completed | partially_synced
```

| Status | Description |
|--------|-------------|
| `processing_completion` | Initial state — handler is running |
| `awaiting_completion_match` | Terminal (parked) — no matching task found, user clarification needed, or error |
| `completed` | Terminal — task(s) successfully matched, closed, and Google-synced |
| `partially_synced` | Terminal — task(s) closed in DB but Google sync failed (queued for retry) |

#### Stage 1: Vault First (Zero Data Loss)

Content is embedded and saved to `memories` *before* any task matching begins. This ensures the information is never lost, regardless of what happens downstream:

- **Embedding validation**: If `get_embedding()` returns null/zero, `embedding_status` is set to `"failed"` instead of lying about being `"embedded"` — the content is still vaulted.
- **Failed queue fallback**: If the memory insert itself throws a DB error, the content is written to the failed queue (`add_to_failed_queue`) for retry instead of being silently dropped.

#### Stage 2: Lexical Prefilter

Fetches all active (non-done, non-cancelled) tasks. If no active tasks exist, parks as `awaiting_completion_match` with reason `no_active_tasks` and notifies the user.

If tasks exist, reduces LLM context by prefiltering: keeps only tasks where at least one significant word from the completion's `title` (>3 chars) appears in the task title. Falls back to top 10 tasks if prefilter returns nothing.

#### Stage 3: LLM Task Matcher

Sends the completion text + candidate tasks to Gemini with a strict JSON matcher prompt. The LLM must return `{"matched_task_ids": [...]}` — IDs that genuinely correspond to the completed task. Empty array if unsure.

**Validation**: Returned IDs are cross-checked against the live active task set. Invalid IDs are discarded — no guessing allowed.

**Fallback chain** (June 2026): The matcher first tries `CLASSIFICATION_MODEL` (Gemini Flash Lite). If that returns no match, it retries once with `SYNTHESIS_MODEL` (Gemini 3.5 Flash) before falling through to user disambiguation. This avoids unnecessary parking on ambiguous phrasing that a more capable model resolves correctly.

#### Stage 4: Disambiguation (Ambiguous Completions)

If no valid task ID is matched after both LLM attempts, the handler checks the candidate count:

- **≤ 5 candidates**: Posts an interactive Telegram message asking the user to pick: `"🧐 Which task did you complete?"` with numbered options + "n" for none. Saves clarification state to conversation history. When the user replies, `resolve_completion_disambiguation()` (`completion_handler.py:235`) accepts:
  - **Digit** (`1`, `2`, …) → picks that numbered candidate
  - **Ordinal word** (`first`, `second`, `third`, …) → mapped to digit
  - **`n` / `none`** → parks as `awaiting_completion_match`
  Routes the choice to `execute_completion_closure()` or parks it.
- **> 5 candidates**: Parks as `awaiting_completion_match` with reason `no_match` — content already vaulted, no further action.

#### Stage 5: Idempotent Closure

`execute_completion_closure()` (`completion_handler.py:167`) runs for every matched task:

1. Reads current task row — skips if already terminal (`done`/`cancelled`)
2. Uses standard `.update()` on the task (Database `BEFORE UPDATE` temporal triggers handle history preservation safely)
3. Writes outcome memory via `write_outcome_memory()` (async, fire-and-forget)
4. Deletes Google Calendar event if present
5. Syncs completion to Google Tasks
6. **Partial failure handling** (June 2026): If any Google sync steps fail, the failed task IDs are collected and surfaced to Telegram with task-level detail (e.g. `"⚠️ Synced 2/3 tasks. Failed: Buy groceries (id=42)"`). The dump status is set to `partially_synced`. Previously, sync failures were swallowed silently.

If all closed IDs were already terminal, parks as `awaiting_completion_match` with reason `already_closed`.

#### Google Calendar 404 Auto-Heal

When `sync_to_calendar` is called for a task whose `google_event_id` references an event that was externally deleted from Google Calendar, the API returns a 404. The handler:

1. Nulls `google_event_id` in the DB *before* re-provisioning (so if re-provision fails, the DB is clean rather than pointing to a ghost).
2. Creates a fresh Google Calendar event and saves the new ID.

Non-404 errors (429, 403, 500) are re-raised — they do **not** null the stored event ID, preventing valid IDs from being cleared on transient failures.

**File**: `core/services/google_service.py` — `sync_to_calendar()`

#### Ownership Boundary

The completion handler **owns** the `awaiting_completion_match` and `partially_synced` statuses. The pulse engine excludes `awaiting_completion_match` from its query scope — it never picks up completion handler dumps. `partially_synced` is included in the pulse scope so the pulse can retry failed Google syncs.

#### Repair Script

`scripts/repair_completions.py` backfills legacy dumps that predate the completion handler. It re-processes old completion-typed raw dumps through the same `handle_confident_completion` flow for idempotent matching and closure.

### Bypass Prefixes

Two prefixes skip intent classification entirely and route directly:

| Prefix | Action |
|--------|--------|
| `N:` or `Note:` | Message → embedded → saved to `memories` as `memory_type='note'` |
| `?` | Message → `interrogate_brain()` — hybrid graph+vector+canonical search (skips classifier) |

This is the fastest interaction path — no Gemini classification call, no staging, no routing.

## The Universal Query Engine (interrogate_brain)

When a message is classified as `QUERY` (or prefixed with `?`), it enters `interrogate_brain()` in `core/webhook/dispatch.py`. This is the unified intelligence system that replaced an older siloed approach.

### Pipeline

1. **Anaphora Resolution** — The query is first rewritten by Gemini to resolve pronouns (e.g., "Who's working on it?" → "Who's working on Solvstrat?") using conversation history. The primary entity is also extracted for anchor scoping.

2. **Active Anchor Scoping** — The bot maintains a session-level `active_anchor` — the last mentioned person/project. If the current query's primary entity differs, the anchor is updated. The anchor scopes the tactical map (`hybrid_search_graph`) to return edges only from that entity's graph node, preventing irrelevant connections.

3. **Date Resolution** — `resolve_dates_from_query()` parses the query for time expressions ("this week", "tomorrow", "June 15") and returns `start_dt`/`end_dt` in IST. If no date is found, both are `None` and the calendar source is skipped.

4. **Source Selection Heuristics** — Three boolean flags (`is_schedule`, `is_comms`, `is_action`) determine which sources to fetch:
   - `is_schedule`: calendar, tasks, people, tactical map
   - `is_comms`: emails, WhatsApp, people, tactical map  
   - `is_action`: tasks, tactical map, serendipity, hindsight
   - If none detected: all 14 sources are fetched

5. **Parallel 14-Source Fetch** — `asyncio.gather` runs all selected sources concurrently:
   - Calendar events (range query, max 14 days, [PAST] tagged)
   - Tactical map (graph edges from active anchor)
   - Active tasks (compressed, high-priority first)
   - Pending decisions (email/call/WhatsApp/graph approvals)
   - Recently completed tasks
   - Vault memories (semantic + graph hybrid)
   - Hindsight memories (multi-signal retrieval)
   - Temporal patterns (on-this-day)
   - Serendipity connections (multi-hop graph paths, max 5)
   - Emails (unread actionable)
   - WhatsApp messages (unread actionable)
   - Resources
   - Practices
   - People network

6. **Proactive Signal Check** — In parallel, `check_proactive_signals()` queries email drafts, pending tasks, and WhatsApp messages for items matching the active anchor. Timeout: 1.5s (fire-and-forget).

7. **Context → Prompt** — All non-empty results are assembled into a unified prompt with:
   - Current time injection (IST, for time-aware responses)
   - Strict output format (no invented headings, answer first, context section second)
   - Max 600 output tokens (hard cap)
   - Mandatory stop sequence instruction

8. **Response** — Gemini Flash Lite generates the answer, which is sent back to Telegram.

## Journal Sync Signal

The webhook also accepts an alternative payload — not from Telegram, but from an external webhook caller — with `{"intent": "JOURNAL_SYNC", "auth_secret": "..."}`. This authenticates via PULSE_SECRET and triggers a GitHub Actions workflow dispatch (`repository_dispatch` with `trigger_pulse` event), starting the full pipeline: archive ingest → graph backfill → pulse briefing.
