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
    return f"""{guards}

You are analyzing a captured note or update to extract actionable signals.
Do NOT filter or drop signals if there are multiple — extract EVERY detectable signal into the list.

Capture: "{text}"{anchor_hint}

Signal Types to Extract:
1. 'deadline' — hard deadline for a specific deliverable, or a scheduled event/meeting at a specific time ("by Monday", "due Friday", "Monday at 11am").
2. 'task_imperative' — explicit directive to create a task ("I need to...", "Remind me to...").
3. 'person_intro' — a new person is introduced with their organization or role.
4. 'financial' — a quote, budget, cost, invoice, or opportunity amount.
5. 'dependency' — multi-step planning or blockers ("discuss with X first").

Return JSON:
{
  "signals": [
    {
      "type": "deadline|task_imperative|person_intro|financial|dependency",
      "title": "Short title describing the signal",
      "raw_date_text": "extracted date phrase (for deadline)",
      "raw_time_text": "extracted time phrase (for deadline)",
      "duration_minutes": 30,
      "proposed_title": "Title for calendar event or task",
      "description": "Additional context",
      "name": "Person name (for person_intro)",
      "org": "Organization (for person_intro)",
      "role": "Role (for person_intro)",
      "task_title": "Task title (for deadline/task_imperative)",
      "raw_deadline_text": "extracted deadline phrase (for deadline)",
      "amount": "numeric amount or string (for financial)",
      "depends_on": "what it depends on (for dependency)",
      "confidence": 0.0-1.0
    }}
  ]
}}"""
