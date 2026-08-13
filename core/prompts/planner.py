"""Action Planner prompt — extracted from core/actions/planner.py for testability.

Single source of truth for the planner prompt. Called by plan_actions() to
generate the LLM prompt that matches user requests to tasks/events/operations.
"""

# Phase 5 (invariant #6): shape-level response schema sent to the providers
# (Gemini responseSchema / OpenRouter json_schema) so the LLM is constrained
# to valid operation names and object-typed params at generation time. Per-op
# required-field enforcement stays in Phase 1's typed models (the strict
# backstop) — this schema deliberately mirrors only the top-level shape.
PLANNER_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "create_task", "create_note", "create_event", "query_info",
                            "close_task", "cancel_recurring", "suppress_instance",
                            "modify_recurring", "reschedule", "update_metadata",
                            "delete_event", "no_op",
                        ],
                    },
                    "target_id": {"type": "string"},
                    "params": {"type": "object"},
                    "human_label": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["operation"],
            },
        }
    },
    "required": ["actions"],
}

def build_planner_prompt(
    current_time: str,
    text: str,
    title: str,
    intent: str | None,
    entity: str,
    candidate_lines: str,
    org_lines: str,
    active_anchor: dict = None,
    resolved_dates: str = "",
    learned_hints: str = "",
) -> str:
    """Build the action planner prompt.

    Args:
        current_time: ISO-8601 timestamp for time reference
        text: Raw user input text
        title: Extracted title from classifier
        intent: Classifier intent (may be None)
        entity: Classifier entity tag
        candidate_lines: Formatted string of candidate tasks/events
        org_lines: Formatted string of available organizations
        active_anchor: Thread's active entity context (name, type, etc.)
        resolved_dates: Text with relative date expressions already resolved
            to absolute dates (Phase 2 — the LLM never computes time math).
        learned_hints: Learning-loop reminders from past clarifications for
            this tenant (empty → section omitted, so the golden is stable).
    """
    # Guard 2: Render active_anchor context so the LLM sees the thread's
    # actual entity (e.g., "FC Madras") rather than just the classifier's
    # routing tag (e.g., "SOLVSTRAT"). Prevents entity context loss.
    # M9.4: the tenant's timezone offset/label (settings → env → IST), so
    # created tasks carry times in the tenant's zone, not Danny's.
    from core.lib.time_utils import tz_label, tz_offset_str
    tz_lbl = tz_label()
    tz_off = tz_offset_str()

    thread_context = ""
    if active_anchor:
        anchor_name = active_anchor.get('name', '')
        anchor_type = active_anchor.get('type', '')
        anchor_org_id = active_anchor.get('last_org_id')
        parts = []
        if anchor_name:
            parts.append(f"Entity: {anchor_name}")
        if anchor_type:
            parts.append(f"Type: {anchor_type}")
        if anchor_org_id:
            parts.append(f"Organization ID: {anchor_org_id}")
        if parts:
            thread_context = "\nTHREAD CONTEXT: " + " | ".join(parts)

    resolved_section = resolved_dates.strip() if resolved_dates and resolved_dates.strip() else "- None detected."
    learned_section = learned_hints.strip() if learned_hints and learned_hints.strip() else ""
    # Prefix with \n so the empty case adds ZERO bytes (the M9.4 golden renders
    # byte-identical when the tenant has no learned patterns yet).
    learned_block = f"\nLEARNED FROM PAST CLARIFICATIONS (MUST-FOLLOW):\n{learned_section}" if learned_section else ""

    return f"""You are an action planner. Match the user's request to the correct tasks/events and operations.
Return ONLY valid JSON: {{"actions": [{{"operation": "create_task|create_note|create_event|query_info|close_task|cancel_recurring|suppress_instance|modify_recurring|reschedule|update_metadata|delete_event|no_op", "target_id": "123", "params": {{"new_reminder_at": "YYYY-MM-DDTHH:MM:SS"}}, "human_label": "Description"}}]}}

CURRENT TIME: {current_time}
{thread_context}

TIME FORMATTING RULES:
- All times MUST be in {tz_lbl} (UTC{tz_off}) using ISO-8601 format.
- "today 3pm" → YYYY-MM-DDT15:00:00{tz_off} (use CURRENT TIME to determine today's date)
- "tomorrow" → YYYY-MM-DD (date only, no time)
- "next Friday 2pm" → compute the date of next Friday and output YYYY-MM-DDT14:00:00{tz_off}
- "6:30 pm today" → YYYY-MM-DDT18:30:00{tz_off}
- Relative deltas ("defer by 7 days", "push it back a week", "in 2 weeks"):
  do NOT compute the date yourself. Output params.time_delta = {{"amount": N, "unit": "days|weeks", "direction": "later|earlier"}} — the system computes the exact timestamp.
- If a RESOLVED_RELATIVE_DATES entry is shown for the user's request, output that absolute date in params.new_reminder_at instead of computing it.
- If no time is given, return null for reminder_at. Do not invent a time.

RESOLVED_RELATIVE_DATES:
{resolved_section}{learned_block}

User text: "{text}"
Extracted intent title: "{title}"
Classifier intent: "{intent or 'UNKNOWN'}"
Entity: "{entity}"

Candidates:
{candidate_lines}

Available Organizations:
{org_lines}

Rules:
- close_task: marks a normal Task as done.
- suppress_instance: skips the next occurrence of a recurring Task.
- cancel_recurring: ends a recurring Task entirely.
- modify_recurring: changes the schedule of a recurring Task (`params.new_rrule` and/or `params.new_reminder_at` / `params.time_delta`). At least one schedule change is required.
- reschedule: changes the time of a non-recurring Task. Provide `params.new_reminder_at` (absolute ISO time) OR `params.time_delta` = {{"amount": N, "unit": "days|weeks|hours", "direction": "later|earlier"}} — e.g. {{"amount": 7, "unit": "days"}} for "defer by 7 days". The system computes the exact timestamp from time_delta; never compute it yourself.
- update_metadata: changes priority or deadline of a Task (`params.new_priority`, `params.new_deadline`).
- delete_event: removes an external Event.
- create_task: creates a new task. Requires `params.title`. For ID resolution, include `params.organization_id` from the lists above. Optional: `params.deadline`, `params.priority`, `params.reminder_at`, `params.rrule`, `params.direction`, `params.committed_to`, `params.duration_mins`.
- create_note: saves information to memory. Requires `params.content`. Optional: `params.organization_name`, `params.organization_id`.
- IMPORTANT: For create_note, do NOT summarize or rewrite the user's content. The original text from document extraction (PyMuPDF) is authoritative and must be preserved verbatim. Your params.content should pass through the key information without losing detail. If the content is already well-structured (meeting notes, action items, decisions), preserve the full structure.
- create_event: schedules a calendar event. Requires `params.title`, `params.time`. Optional: `params.duration_mins`.
- query_info: fetches information from the brain to answer the user's question. Requires `params.query`.
- target_id MUST be the exact numeric ID for existing Tasks, or string ID for existing Events. Not used for create operations.
- Task operations (close_task, cancel_recurring, etc.) MUST use the numeric Task ID. Event IDs can ONLY be used with delete_event.
- IMPORTANT: A recurring task with status 'done' or 'todo' is STILL AN ACTIVE SERIES. 'done' only skips the current week. If the user asks to cancel a recurring series, target ALL matching recurring tasks regardless of their current status.
- If the user uses words like "all", "meetings", or "tasks" (plural), return a separate action for EVERY matching candidate.
- IMPORTANT EXPLICIT INTENTS: If the Classifier intent is NOTE, you MUST output a create_note action. If the Classifier intent is TASK, you MUST output a create_task action. If the Classifier intent is COMPLETION, you MUST output a close_task action for the matching task ID. Do not require an explicit user command in these cases.
- For mixed or informational content (status updates, team changes, finance mentions, decisions, meeting fallout): If the classifier intent is NOTE, ALWAYS route as create_note — do NOT split into multiple tasks. If the classifier intent is TASK, create the task but include informational context in params.content.
- Never make up or hallucinate details not in the user's message. Every field in params (title, reminder_at, priority, deadline, etc.) must be directly derived from the user's text. Do not infer, guess, or fill in defaults that the user did not provide.
- Vaulted tasks: tasks with deadlines or reminders more than 2 days in the future are "vaulted" by the horizon guard. "Pulling forward" means updating the deadline/reminder to be within the next 2 days so the task appears in the active board. If the user asks about vaulted items or says "pull forward", use modify_recurring or reschedule as appropriate to set the deadline/reminder closer.
- Return empty array or no_op if nothing matches."""
