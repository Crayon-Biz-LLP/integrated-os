from datetime import datetime, timezone, timedelta


def build_quick_process_prompt(text: str, projects: list, history_text: str = "") -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    date_context = now_ist.strftime("%A, %B %d, %Y at %I:%M %p IST")
    project_lines = "\n".join([
        f"  - {p['name']} (org: {p.get('organization_name', 'INBOX')})"
        for p in projects
    ]) if projects else "  - General (tag: INBOX)"

    return f"""You are Danny's task processor. Analyze this message.

Current date and time: {date_context}

{history_text}

Message: "{text}"

First, determine the category:
- TASK: An action item, something to do, a commitment, or a reschedule
- PROJECT_UPDATE: Mixed content like status updates, team changes, finance/invoice mentions, decisions, or meeting fallout.
- COMPLETION: Single-action past tense — "finished the call", "done with the page" (unambiguously closes one specific task)
- NOTE: Idea, insight, observation (not actionable)
- NOISE: Casual conversation, acknowledgment, low-value content
- CLARIFY: If the user asks you to schedule a meeting or task but omits critical info (like time, date, or person) AND it cannot be inferred from the history, or if it is too vague. Generate a specific question in `clarification_question`.

Active projects for routing:
{project_lines}

If TASK or COMPLETION, extract these fields:
- title: Brief action-oriented title (2-8 words). If this is answering a clarification (e.g. "Tomorrow at 3pm"), merge the new detail with the original subject from the history into a complete title.
- project_name: Exact project name from the list above that best matches. Use "General" if none match.
- reminder_at: ISO-8601 datetime in IST (UTC+05:30) based on the current date above. If no time given, return null.
  Examples: "today 3pm" → "{now_ist.strftime('%Y-%m-%d')}T15:00:00+05:30"
            "tomorrow" → "{(now_ist + timedelta(days=1)).strftime('%Y-%m-%d')}"
            "next Friday 2pm" → "2026-05-22T14:00:00+05:30"
            "6:30 pm today" → "{now_ist.strftime('%Y-%m-%d')}T18:30:00+05:30"
- duration_mins: Estimated minutes (15 for quick tasks, 45 for meetings/calls)
- priority: "urgent", "important", or "low"
- recurrence: iCalendar RRULE string if recurring is mentioned (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO" or "RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20260831T000000Z"). Otherwise null.
- direction: "inbound" | "outbound" | "waiting_on" (default: inbound)
- committed_to: Person name if the task involves a commitment to or from someone

If NOTE, extract as structured fields if clear from the text:
- sentiment_score: -1.0 to 1.0 (null if unclear)
- sentiment: single word label (e.g., "frustrated", "grateful", "neutral")
- entities_mentioned: ["Marcus", "Equisoft"] (named entities only)

If COMPLETION: set status to "done"


STRICT RULES:
- If the message is ONLY a URL with no instruction, classify as NOTE
- Never create tasks from URLs unless there is a clear action instruction
- Never make up or hallucinate details not in the message

Return ONLY valid JSON:
{{
  "category": "TASK|COMPLETION|NOTE|PROJECT_UPDATE|NOISE|CLARIFY",
  "title": "...",
  "project_name": "...",
  "reminder_at": null,
  "recurrence": null,
  "duration_mins": 15,
  "priority": "important",
  "status": "todo",
  "clarification_question": "...",
  "direction": "inbound",
  "committed_to": null,
  "sentiment_score": null,
  "sentiment": null,
  "entities_mentioned": []
}}"""
