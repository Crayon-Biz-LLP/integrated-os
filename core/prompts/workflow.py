from datetime import datetime
from zoneinfo import ZoneInfo

from core.prompts.guards import inject_guards
import json

def build_workflow_resume_prompt(w_type: str, payload: dict, text: str) -> str:
    # This is an internal routing prompt, no action guard needed.
    return f"""You are evaluating a user's reply to a pending proposed action.
Proposed Action Type: "{w_type}"
Proposed Details: {json.dumps(payload)}

User's Reply: "{text}"

CRITICAL: Return "unrelated" if the message is about a different project, person, task, or organization than the pending workflow. Only return "confirm" or "decline" when the reply is about the SAME subject as the proposed action above. Ambiguous acknowledgments with no topical evidence (e.g. "ok", "sure") may still be treated as confirm/decline based on conversational context.

Did the user explicitly confirm/agree to proceed with the action?
Did the user explicitly decline/cancel it?
Or is this an entirely unrelated message that ignores the proposal?

Return JSON:
{{
  "decision": "confirm" | "decline" | "unrelated"
}}"""

def build_enrichment_prompt(text: str, anchor_hint: str) -> str:
    guards = inject_guards("enrichment")
    ist_tz = ZoneInfo('Asia/Kolkata')
    now_str = datetime.now(ist_tz).strftime("%A %Y-%m-%d %I:%M %p %Z")
    return f"""{guards}

Current time: {now_str}

You are analyzing a captured note or update to extract actionable signals.
Do NOT filter or drop signals if there are multiple — extract EVERY detectable signal into the list.

Capture: "{text}"{anchor_hint}

Signal Types to Extract:
1. 'calendar_event' — a specific scheduled event, meeting, call, or discussion at a specific time. Include the resolved time in "reminder_at" as ISO 8601 (YYYY-MM-DDTHH:MM:SS+05:30).
2. 'deadline' — hard deadline for a specific deliverable ("by Monday evening", "due Friday"). Include the resolved time in "reminder_at" as ISO 8601.
3. 'task_imperative' — explicit directive to create a task ("I need to...", "Remind me to...").
4. 'person_intro' — a new person is introduced with their organization or role.
5. 'financial' — a quote, budget, cost, invoice, or opportunity amount.
6. 'dependency' — multi-step planning or blockers ("discuss with X first").

Return JSON:
{{
  "signals": [
    {{
      "type": "calendar_event|deadline|task_imperative|person_intro|financial|dependency",
      "title": "Short title describing the signal",
      "reminder_at": "ISO 8601 datetime (for calendar_event/deadline, use Current time above to resolve relative dates)",
      "duration_minutes": 30,
      "proposed_title": "Title for calendar event or task",
      "description": "Additional context",
      "name": "Person name (for person_intro)",
      "org": "Organization (for person_intro)",
      "role": "Role (for person_intro)",
      "task_title": "Task title (for calendar_event/deadline/task_imperative)",
      "amount": "numeric amount or string (for financial)",
      "depends_on": "what it depends on (for dependency)",
      "confidence": 0.0-1.0
    }}
  ]
}}"""
