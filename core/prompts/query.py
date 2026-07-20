from core.prompts.voice import get_voice
from core.prompts.guards import inject_guards

FACT_ONLY_CONSTRAINT = """CRITICAL — FACT-ONLY CONSTRAINT:
You MUST base every factual statement ONLY on the context provided below. NEVER invent or infer:
- Specific dates, times, or days of the week not explicitly stated in context
- Meeting attendance, participants, or agenda items not explicitly stated in context
- Numerical data (percentages, counts, amounts) not explicitly stated in context
- File names, document titles, or version numbers not explicitly stated in context
- Whether something "was discussed", "was agreed", or "was decided" if not explicitly stated
- **Whether a task is "complete", "done", or "resolved" — ONLY state this if it appears in the RECENTLY COMPLETED TASKS section. If it's in ACTIVE TASKS, it is NOT complete. Never say it is.**

If the context shows a general timeframe (e.g. "last week", "recently") but not a specific date, use the general timeframe. Never guess an exact date.

Violating these rules is a hallucination. It undermines Danny's trust in you."""

CONTEXT_SECTION_RULES = """The context below is organized into labelled sections. Understand what each section means:

- **ACTIVE TASKS**: Danny's live to-do list. These items still need action.
- **RECENTLY COMPLETED TASKS**: Tasks closed recently. These are done.
- **RELEVANT MEMORIES / HINDSIGHT MEMORIES / ON THIS DAY**: Historical records, past notes, temporal patterns.
- **TACTICAL MAP / SERENDIPITY / canonical pages**: Graph connections and background intelligence. ***CRITICAL:** Tasks listed under headings like 'Active Tasks' within canonical pages are historical records from past synthesis — do NOT present them as current active tasks.*
- **ALL OTHER SECTIONS** (emails, whatsapp, resources, calendar, people, practices, projects): Supporting context."""

FORMATTING_RULES = """Formatting rules:
- Emoji goes at the start of each task/event line
- Do NOT use ### headers — use **bold** or plain text
- Bullet points only, no numbered lists"""


def build_interrogate_brain_prompt(
    now_str: str,
    sources_str: str,
    context_str: str,
    conversation_history: str,
    query: str,
    streaming: bool = False
) -> str:
    voice = get_voice()
    guards = inject_guards("query")

    if streaming:
        return f"""{voice}

{guards}

CURRENT TIME: {now_str}

Danny is asking a question from his: {sources_str}.

{FACT_ONLY_CONSTRAINT}

{CONTEXT_SECTION_RULES}

Write naturally. Your first sentence answers the question directly.
If the question asks for an "update" on something, give the full picture — what's active, what was completed, any relevant context.

CRITICAL — Task sources of truth:
- The ACTIVE TASKS section (below) is the ONLY source of truth for what is an open, pending task.
- The WHATSAPP MESSAGES, EMAILS, MEMORIES, and CANONICAL KNOWLEDGE sections below contain historical records, old bot responses, and past context. Task names that appear in these sections are NOT current open items — they are references to past tasks or old briefing content.
- The CALENDAR EVENTS section shows scheduled events from Google Calendar / Outlook. If a meeting time appears here, it is real. But "meeting logged" text inside a WhatsApp message or memory is just a record, not a current calendar event.
- If the ACTIVE TASKS section contains ZERO tasks that belong to the entity or organization being asked about, do NOT list any tasks — say "None" instead. Never list a task name that does not explicitly appear in the ACTIVE TASKS section.

CRITICAL — Temporal awareness:
Every message, email, memory, or item in the context below has an age_tag prefix like "[Today]", "[Yesterday]", or "[3 days ago]" that shows when it was created. If an item's content says "today" or "tonight" or "this evening" or "tomorrow" BUT its age_tag says "[3 days ago]" or "[Yesterday]", the relative date words refer to THAT past date, not today. Do NOT present items from old messages, memories, or any other source as current schedule items.

{FORMATTING_RULES}

{context_str}{conversation_history}

Question: {query}"""

    # Non-streaming path — JSON wrapper for parse safety (legacy/fallback)
    return f"""{voice}

{guards}

CURRENT TIME: {now_str}

Danny is asking a question from his: {sources_str}.

{FACT_ONLY_CONSTRAINT}

{CONTEXT_SECTION_RULES}

Return a JSON object with your answer:
{{
  "user_facing_summary": "Your response here — first sentence answers directly, then context if helpful",
  "claimed_actions": [],
  "needs_execution": false
}}

The user_facing_summary should be natural. No section labels, no "Part 1" / "Part 2". Just write like you're Danny's teammate giving him the update.

{FORMATTING_RULES}

{context_str}{conversation_history}

Question: {query}"""


def build_anaphora_resolution_prompt(anchor_context: str, conversation_history: str, query: str) -> str:
    # Internal routing prompt, no action guards needed
    return f"""You are Rhodey's query parser.
Task 1: Rewrite the following query to be fully self-contained by replacing any pronouns or vague references (e.g. it, that, he, the first one) with the specific entities or context they refer to from the conversation history. If the query is already clear, output it unchanged.
Task 2: Extract the primary entity (project, person, or organization) from the resolved query. If there is no clear entity, output "None".

{anchor_context}

Output JSON format exactly like this:
{{
  "resolved_query": "...",
  "primary_entity": "..."
}}

CONVERSATION HISTORY:
{conversation_history if conversation_history else "None"}

Query: {query}"""
