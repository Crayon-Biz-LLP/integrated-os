# 9. The Task Creation System

## The 3+1 Task Creation Paths

Tasks enter the `tasks` table through exactly 3 direct INSERT paths and 1 indirect path. Each serves a different purpose and has different side effects.

### Path 1: Quick Process (Inline Single INSERT)

**Trigger**: Telegram message classified as TASK, or email pending task approved.

**Flow**:
```
Telegram text → classify → handle_confident_task()
    → insert into raw_dumps (status='pending')
    → process_single_dump() [called inline, same async context]
        → build_combined_prompt() with active projects
        → Gemini classifies: TASK/COMPLETION/NOTE/NOISE
        → if TASK: insert into tasks table
        → sync_to_calendar() (Google Calendar, if time given)
        → sync_to_google() (Google Tasks, if time given)
        → update task with google IDs
```

**File**: `core/agents/quick_process.py:159`

**Key characteristics**:
- Single task, created immediately
- Uses inline Gemini call with project context
- Syncs to Google Calendar (if time specified) and Google Tasks
- DOES create graph edges async (links to Projects/People via write_graph_edges_for_task)
- DOES Semantic Deduplication (check_duplicate) to safely convert overlaps into Task Updates
- DOES calendar collision checking (check_conflict) and returns warnings to the webhook
- Also runs from `process_pending_dumps()` every 5 minutes via GitHub Actions

### Path 2: Pulse Engine Batch INSERT

**Trigger**: Scheduled Pulse briefing, AI includes tasks in `new_tasks` array.

**Flow**:
```
process_pulse() → AI generates PulseOutput JSON
    → iterates ai_data['new_tasks']
    → for each task:
        → resolve project via 7-stage cascade
        → build task_insert dict
    → batch INSERT via supabase.table('tasks').insert(task_inserts)
    → for each task (async background):
        → write_graph_edges_for_task() (graph node + BELONGS_TO + INVOLVES)
        → sync_to_google() (Google Tasks)
        → sync_to_calendar() (Google Calendar, if explicit time)
        → update task with google IDs
```

**File**: `core/pulse/engine.py:1411`

**Key characteristics**:
- Batch insert (multiple tasks from one AI response)
- Graph edges ARE created inline (task node + BELONGS_TO project + INVOLVES people)
- Same Google Calendar/Tasks sync as Path 1
- Handles conflict de-clash (15-min stagger)

### Path 3: Temporal Lineage Versioned INSERT

**Trigger**: Google Tasks reports a task as completed externally.

**Flow**:
```
process_pulse() → sync_completed_tasks_from_google()
    → queries tasks table WHERE google_task_id IS NOT NULL AND status='todo'
    → for each task found completed in Google Tasks:
        → create_versioned_task()
            → marks old task is_current=False
            → INSERT new task with status='done', version+1, supersedes_id=old_id
```

**File**: `core/lib/temporal_lineage.py:102`

**Key characteristics**:
- Creates a versioned COPY of an existing task (not a new action)
- Marks the original as superseded (`is_current=False`)
- Does NOT sync to Google (this is reading FROM Google)
- Does NOT create graph edges
- Ensures the local database is always in sync with Google's canonical state

### Path 4: Email Pending Decision (Indirect)

**Trigger**: User approves an email-pending task via Telegram (`"5 yes"`) or Web UI.

**Flow**:
```
process_email_pending_decision(id, 'approve')
    → checks duplicate via is_already_in_tasks_table()
    → inserts into raw_dumps (status='pending', source='email')
    → later picked up by process_pending_dumps() (Path 1)
    → process_single_dump() creates the actual task
```

**File**: `core/webhook/email.py:104` (raw_dump insert)

**Key characteristics**:
- Two-phase: first raw_dump, then later → task
- Duplicate guard runs at approval time (can merge with existing task)
- If marked as possible_duplicate, the raw_dump carries a flag

## Summary

| Path | Insert Location | Mode | Graph Edges? | Calendar? | Tasks? | Trigger |
|------|----------------|------|-------------|-----------|--------|---------|
| Quick Process | quick_process.py:210 | Single inline | Yes (Async) | Yes | Yes | Telegram task / 5-min cron |
| Pulse Batch | engine.py:1411 | Batch | Yes | Yes | Yes | Scheduled pulse |
| Temporal Lineage | temporal_lineage.py:102 | Single versioned | No | No | No | Google Tasks external completion |
| Email Indirect | email.py:104 → quick_process.py | Two-phase indirect | No | Yes | Yes | User email approval |

## Recurrence & Clarification Loops
- **Conversational `CLARIFY`:** Both Webhook intake and `quick_process.py` support interactive disambiguation loops. If critical info (like time) is missing, Rhodey will halt extraction, ask the user on Telegram, and pass `history_text` on the second pass so the task is created perfectly.
- **Recurring Tasks (`UNTIL` Support):** The `TASK` extraction models support infinite and finite recurrences. If the user says "every Wednesday until August 31", the system generates an iCalendar RRULE (e.g., `RRULE:FREQ=WEEKLY;UNTIL=20260831T000000Z`) and pushes it directly to Google Calendar. Recurring tasks handle status changes as follows:
  - **`done`** → skips the next Google Calendar instance, writes an outcome memory, but leaves the task as `todo` — the series continues
  - **`cancelled`** → deletes the entire series from Google Calendar, marks the task as `cancelled` in the DB
  - **Auto-expiry:** The pulse engine's `_auto_expire_recurring_tasks()` checks the RRULE `UNTIL` date (supports both `YYYYMMDDTHHMMSSZ` and `YYYYMMDD` formats) and `COUNT`-based recurrences, marks expired tasks as `cancelled`
  - **Skip instance:** The `skip_recurring_instance(task_id, date_str)` tool deletes the next individual Google Calendar instance without affecting the series
  - **Reschedule ambiguity:** When the user asks to reschedule a recurring event, the AI pauses to clarify: skip+create standalone vs shift entire series

## Google Calendar Sync Behavior

When tasks are synchronized to Google Calendar, the system dynamically applies visual priority prefixes to the event summary (title). This allows the user to gauge the urgency of an event immediately from the calendar grid.

### Priority Prefixes
- **Urgent**: `🔥 CRITICAL: [Task Title]`
- **Low**: `☕ INFO: [Task Title]`
- **Default/Important**: `⚡ ACTION: [Task Title]`

### Event Description
All synchronized events include the hardcoded description: `"Rhodey created this for you."`

This behavior is applied consistently across all task creation paths (Quick Process, Pulse Batch, Email Approvals) via `core/services/google_service.py` and `core/pulse/calendar.py`.

*Note: To prevent prefix duplication upon updates, the system strips any existing prefixes before applying the correct one.*
