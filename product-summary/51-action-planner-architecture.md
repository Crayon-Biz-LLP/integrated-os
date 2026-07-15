# 51 - Action Planner Architecture

## Overview
Rhodey's single-intent routing architecture has been upgraded with a universal Action Planner. Previously, completion handling was bottlenecked by a single LLM matching path that only queried open tasks. The new Action Planner enables multi-source, multi-operation execution for natural language commands affecting tasks, recurring series, and raw calendar events.

In **Phase 52 (Holistic Architecture Completion)**, the Action Planner became the **single unified pipeline** for all task/note/completion operations — replacing the three-headed architecture (Webhook + Quick Process cron + Pulse Engine staging sorter). All 6 former `process_single_dump` callers now route through `plan_actions()` → `execute_planned_actions()`.

## Motivation & Crash Fix
The legacy completion matcher had a fatal flaw: if no open task matched the input, it entered a degradation path (`compute_pattern_confidence`) which relied on `SlidingWindowLimiter`. This limiter used a synchronous `threading.Lock` over a synchronous Redis HTTP check, blocking the async event loop and causing a silent 60-second Vercel timeout on unmatched complex requests (like cancelling a recurring series that was already marked 'done' for the week).

The Action Planner eliminates this degradation path entirely by shifting the matching responsibility to a multi-source planner that always returns typed actions (or `no_op`).

## The Action Model
The architecture centers around a Typed Action Model (`core/actions/models.py`):
```python
@dataclass
class Action:
    operation: Operation
    target_id: Optional[Union[int, str]] = None
    params: dict = field(default_factory=dict)
    confidence: float = 1.0
    human_label: str = ""
```

### Supported Operations
1. `close_task`: Standard task completion.
2. `cancel_recurring`: Ends a recurring series entirely (`status="cancelled"`).
3. `suppress_instance`: Skips the next occurrence (`status="done"` on a recurring task).
4. `modify_recurring`: Changes schedule, computing new `params.new_rrule` and `params.new_reminder_at`.
5. `reschedule`: Changes date/time of non-recurring task without closing it.
6. `update_metadata`: Modifies priority or deadline.
7. `delete_event`: Directly deletes a raw Google Calendar event (for events lacking a task record).

## Multi-Source Candidate Pool
To give the LLM the correct context to resolve ambiguity (e.g., "Cancel the Armour meeting"), the Planner constructs a candidate pool querying three data planes simultaneously:
1. **Active Tasks**: Standard `todo` / `in_progress` tasks.
2. **Active Recurring Tasks**: Tasks with an RRULE, even if their current status is `done` (since `done` only skips the current instance, the series remains targetable).
3. **14-Day Calendar Window**: Raw Google Calendar events via `get_upcoming_calendar_events()` to catch entries created outside of Rhodey.

## Execution Flow
1. User sends message -> Classified as `COMPLETION`.
2. `plan_actions()` (in `core/actions/planner.py`) compiles the candidate pool and lexically pre-filters it.
3. The LLM (Flash Lite / Flash 3.5 fallback) resolves the text against the candidates and returns a JSON array of `Action` objects.
4. `core/webhook/completion_handler.py` executes the operations:
   - Modifies `tasks` table via `update_task_status()` (which syncs to Calendar).
   - Patches `tasks` metadata directly.
   - Deletes Calendar events directly.

## Resilience Enhancements
- **Async Locks**: Rate limiters now use `asyncio.Lock` and `asyncio.to_thread()` for Redis checks, preventing event loop starvation.
- **Vercel Timeout Net**: The top-level `process_webhook()` is now wrapped in `asyncio.wait_for(..., timeout=55)`. If the request hits 55s, Rhodey intercepts the kill signal, returns a 200 OK to Telegram, and asynchronously sends a "still thinking" placeholder to the user, preventing silent deaths and preserving the audit trail.
