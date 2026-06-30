from core.prompts.guards import inject_guards
from datetime import datetime, timezone, timedelta

def build_quick_process_prompt(text: str, projects: list, history_text: str = "") -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    date_context = now_ist.strftime("%A, %B %d, %Y at %I:%M %p IST")
    project_lines = "\n".join([
        f"  - {p['name']} (org: {p.get('organization_name', 'INBOX')})"
        for p in projects
    ]) if projects else "  - General (tag: INBOX)"

    guards = inject_guards("ingest")

    return f"""{guards}

You are Danny's task processor. Analyze this message.

Current date and time: {date_context}

{history_text}

Message: "{text}"

First, determine the category:
- TASK: An action item, something to do, a commitment, or a reschedule
- PROJECT_UPDATE: Mixed content like status updates, team changes, finance/invoice mentions, decisions, or meeting fallout.
- COMPLETION: Single-action past tense — "finished the call", "done with the page" (unambiguously closes one specific task)
- NOTE: Idea, insight, observation (not actionable)

Then extract the requested fields for that category.
Return ONLY a valid JSON object matching the chosen category.

Options for project names (pick the closest match or use "INBOX" if none fit):
{project_lines}

If TASK:
{{
  "category": "TASK",
  "title": "Clear action statement (start with verb)",
  "project": "One of the provided project names or INBOX",
  "duration_mins": 15,
  "priority": "normal|important|urgent",
  "direction": "inbound|outbound|waiting_on",
  "committed_to": "Person or organization name if this is a promise to them (or null)",
  "reminder_at": "YYYY-MM-DDTHH:MM:00+05:30 (Only if a specific time is mentioned or heavily implied. Must be future)",
  "explicit_time": true/false (true ONLY if the user explicitly said "remind me at X", "tomorrow at Y", etc.)
}}

If PROJECT_UPDATE or NOTE:
{{
  "category": "PROJECT_UPDATE", // or "NOTE"
  "sentiment": "positive|neutral|negative|frustrated|excited",
  "sentiment_score": 0.0 to 1.0,
  "entities_mentioned": ["Name1", "Name2", "Tool1"] // Array of people, companies, tools
}}

If COMPLETION:
{{
  "category": "COMPLETION",
  "title": "What was completed (e.g. 'Status sync call', 'Pricing page')"
}}

RULES:
- Never create tasks from URLs unless there is a clear action instruction.
- Never make up or hallucinate details not in the message.
- If it's a reschedule, set category=TASK and reminder_at=the new time."""
