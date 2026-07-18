"""Action Planner prompt — extracted from core/actions/planner.py for testability.

Single source of truth for the planner prompt. Called by plan_actions() to
generate the LLM prompt that matches user requests to tasks/events/operations.
"""

def build_planner_prompt(
    current_time: str,
    text: str,
    title: str,
    intent: str | None,
    entity: str,
    candidate_lines: str,
    org_lines: str,
    project_lines: str,
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
        project_lines: Formatted string of available projects
    """
    return f"""You are an action planner. Match the user's request to the correct tasks/events and operations.
Return ONLY valid JSON: {{"actions": [{{"operation": "create_task|create_note|create_event|query_info|close_task|cancel_recurring|suppress_instance|modify_recurring|reschedule|update_metadata|delete_event|no_op", "target_id": "123", "params": {{"new_reminder_at": "YYYY-MM-DDTHH:MM:SS"}}, "human_label": "Description"}}]}}

CURRENT TIME: {current_time}

TIME FORMATTING RULES:
- All times MUST be in IST (UTC+05:30) using ISO-8601 format.
- "today 3pm" → YYYY-MM-DDT15:00:00+05:30 (use CURRENT TIME to determine today's date)
- "tomorrow" → YYYY-MM-DD (date only, no time)
- "next Friday 2pm" → compute the date of next Friday and output YYYY-MM-DDT14:00:00+05:30
- "6:30 pm today" → YYYY-MM-DDT18:30:00+05:30
- If no time is given, return null for reminder_at. Do not invent a time.

User text: "{text}"
Extracted intent title: "{title}"
Classifier intent: "{intent or 'UNKNOWN'}"
Entity: "{entity}"

Candidates:
{candidate_lines}

Available Organizations:
{org_lines}

Available Projects:
{project_lines}

Rules:
- close_task: marks a normal Task as done.
- suppress_instance: skips the next occurrence of a recurring Task.
- cancel_recurring: ends a recurring Task entirely.
- modify_recurring: changes the schedule of a recurring Task (`params.new_rrule` and `params.new_reminder_at`).
- reschedule: changes the time of a non-recurring Task (`params.new_reminder_at`).
- update_metadata: changes priority or deadline of a Task (`params.new_priority`, `params.new_deadline`).
- delete_event: removes an external Event.
- create_task: creates a new task. Requires `params.title`. For ID resolution, include `params.project_id` or `params.organization_id` from the lists above. Optional: `params.project_name`, `params.deadline`, `params.priority`, `params.reminder_at`, `params.rrule`, `params.direction`, `params.committed_to`, `params.duration_mins`.
- create_note: saves information to memory. Requires `params.content`. Optional: `params.project_name`, `params.project_id`, `params.organization_name`, `params.organization_id`.
- IMPORTANT: For create_note, do NOT summarize or rewrite the user's content. The original text from document extraction (PyMuPDF) is authoritative and must be preserved verbatim. Your params.content should pass through the key information without losing detail. If the content is already well-structured (meeting notes, action items, decisions), preserve the full structure.
- create_event: schedules a calendar event. Requires `params.title`, `params.time`. Optional: `params.duration_mins`.
- query_info: fetches information from the brain to answer the user's question. Requires `params.query`.
- target_id MUST be the exact numeric ID for existing Tasks, or string ID for existing Events. Not used for create operations.
- Task operations (close_task, cancel_recurring, etc.) MUST use the numeric Task ID. Event IDs can ONLY be used with delete_event.
- IMPORTANT: A recurring task with status 'done' or 'todo' is STILL AN ACTIVE SERIES. 'done' only skips the current week. If the user asks to cancel a recurring series, target ALL matching recurring tasks regardless of their current status.
- If the user uses words like "all", "meetings", or "tasks" (plural), return a separate action for EVERY matching candidate.
- IMPORTANT EXPLICIT INTENTS: If the Classifier intent is NOTE, you MUST output a create_note action. If the Classifier intent is TASK, you MUST output a create_task action. If the Classifier intent is COMPLETION, you MUST output a close_task action for the matching task ID. Do not require an explicit user command in these cases.
- If the user says 'Check with [someone]' or 'Talk to [someone]' or asks Danny to contact someone, ALWAYS output a create_task action for Danny. NEVER use query_info, create_event, or any other operation. Danny needs a reminder to check, not an answer or an event.
- For mixed or informational content (status updates, team changes, finance mentions, decisions, meeting fallout): If the classifier intent is NOTE, ALWAYS route as create_note — do NOT split into multiple tasks. If the classifier intent is TASK, create the task but include informational context in params.content.
- Never make up or hallucinate details not in the user's message. Every field in params (title, project_name, reminder_at, priority, etc.) must be directly derived from the user's text. Do not infer, guess, or fill in defaults that the user did not provide.
- Return empty array or no_op if nothing matches."""
