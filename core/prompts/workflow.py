from core.prompts.guards import inject_guards
import json

def build_workflow_resume_prompt(w_type: str, payload: dict, text: str) -> str:
    # This is an internal routing prompt, no action guard needed.
    return f"""You are evaluating a user's reply to a pending proposed action.
Proposed Action Type: "{w_type}"
Proposed Details: {json.dumps(payload)}

User's Reply: "{text}"

CRITICAL: Only return "confirm" or "decline" if the user's reply is about the SAME project, task, person, or subject as the proposed action above. If the user mentions a different entity or an unrelated topic, return "unrelated". Ambiguous acknowledgments with no topical evidence (e.g. "ok", "sure") may still be treated as confirm/decline based on conversational context.

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

You are analyzing a captured note or update.
Capture: "{text}"{anchor_hint}

Does this capture contain an EXPLICIT imperative asking to create a task? (e.g., "Remind me to...", "Add a task to...", "I need to..."). If so, set needs_task=true.
If not, does it leave exactly ONE critical ambiguity that requires a targeted question (e.g., "Who else should know?", "Is this pricing or scope?", "Do you want me to add a task for X?")?
If neither, or if there are too many actions, just acknowledge.

If `needs_question` is true and you're asking for permission to take an action (e.g., "Do you want me to add a task for X?"), set `proposed_workflow` to "task_creation" or "calendar_event" and include the title in `proposed_payload`. If the question is purely for disambiguation (e.g., "Who else should know?"), set `proposed_workflow` to "awaiting_disambiguation_confirmation".

Return JSON:
{{
  "needs_task": boolean,
  "suggested_task_title": "string or null",
  "needs_question": boolean,
  "suggested_question": "string or null",
  "proposed_workflow": "string or null",
  "proposed_payload": {{}}
}}"""
