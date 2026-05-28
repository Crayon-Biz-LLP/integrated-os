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

### Stage 4: Shortcode Decision Resolution

Before the standard classification path, the system checks for shortcode patterns — one of the fastest interaction paths:

```python
# Match patterns like "5 yes", "12 drop", "3 no"
# Routes to process_email_pending_decision(shortcode, action)
# Also handles "/drop-<practice>" for practice dismissal
match = re.match(r'^(\d{1,4})\s+(yes|no|drop|approve|reject)$', text)
```

This allows the user to approve/reject email-suggested tasks or dismiss detected spiritual practices with simple numeric replies — no menus, no navigation.

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
| TASK | `handle_confident_task()` | Insert raw_dump, inline process via `process_single_dump()` (Includes Semantic Guard + Graph Edges) |
| NOTE | `handle_confident_note()` | Insert raw_dump (staged), embed, save to memories |
| COMPLETION | `handle_confident_completion()` | Embed → save to memories, LLM match to open task, close + Google sync, or park as `awaiting_completion_match` |
| QUERY | `interrogate_brain()` | Hybrid graph + vector + canonical search |
| DAILY_BRIEF | `handle_daily_brief()` | Google + Outlook calendar, active tasks, overdue, Gemini brief |
| DELEGATE | agent_queue insert | Research agent picks up via background worker |
| DECLARE_PRACTICE | `handle_declare_practice()` | Create practice graph node with full metadata |
| NOISE | `handle_noise()` | Send 👍 |
| CLARIFICATION_NEEDED | Clarification question | Ask user to disambiguate |

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

#### Stage 4: Disambiguation (Ambiguous Completions)

If no valid task ID is matched, the handler checks the candidate count:

- **≤ 5 candidates**: Posts an interactive Telegram message asking the user to pick: `"🧐 Which task did you complete?"` with numbered options + "n" for none. Saves clarification state to conversation history. When the user replies with a digit or "n", `resolve_completion_disambiguation()` (`completion_handler.py:235`) routes the choice to `execute_completion_closure()` or parks it.
- **> 5 candidates**: Parks as `awaiting_completion_match` with reason `no_match` — content already vaulted, no further action.

#### Stage 5: Idempotent Closure

`execute_completion_closure()` (`completion_handler.py:167`) runs for every matched task:

1. Reads current task row — skips if already terminal (`done`/`cancelled`)
2. Uses `versioned_update()` for safe concurrent writes
3. Writes outcome memory via `write_outcome_memory()` (async, fire-and-forget)
4. Deletes Google Calendar event if present
5. Syncs completion to Google Tasks
6. If sync fails, parks dump as `partially_synced` and queues for retry

If all closed IDs were already terminal, parks as `awaiting_completion_match` with reason `already_closed`.

#### Ownership Boundary

The completion handler **owns** the `awaiting_completion_match` and `partially_synced` statuses. The pulse engine excludes `awaiting_completion_match` from its query scope — it never picks up completion handler dumps. `partially_synced` is included in the pulse scope so the pulse can retry failed Google syncs.

#### Repair Script

`scripts/repair_completions.py` backfills legacy dumps that predate the completion handler. It re-processes old completion-typed raw dumps through the same `handle_confident_completion` flow for idempotent matching and closure.

### Bypass Prefixes

Two prefixes skip intent classification entirely and route directly:

| Prefix | Action |
|--------|--------|
| `N:` or `Note:` | Message → embedded → saved to `memories` as `memory_type='note'` |
| `?` | Message → hybrid graph+vector+canonical search → Gemini synthesis |

This is the fastest interaction path — no Gemini classification call, no staging, no routing.

## Journal Sync Signal

The webhook also accepts an alternative payload — not from Telegram, but from an external webhook caller — with `{"intent": "JOURNAL_SYNC", "auth_secret": "..."}`. This authenticates via PULSE_SECRET and triggers a GitHub Actions workflow dispatch (`repository_dispatch` with `trigger_pulse` event), starting the full pipeline: archive ingest → graph backfill → pulse briefing.
