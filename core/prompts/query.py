from core.prompts.voice import get_voice
from core.prompts.guards import inject_guards

FACT_ONLY_CONSTRAINT = """CRITICAL — FACT-ONLY CONSTRAINT:
You MUST base every factual statement ONLY on the context provided below. NEVER invent or infer:
- Specific dates, times, or days of the week not explicitly stated in context
- Meeting attendance, participants, or agenda items not explicitly stated in context
- Numerical data (percentages, counts, amounts) not explicitly stated in context
- File names, document titles, or version numbers not explicitly stated in context
- Whether something "was discussed", "was agreed", or "was decided" if not explicitly stated
- **Whether a task is "complete", "done", or "resolved" unless it appears in the RECENTLY COMPLETED TASKS section**

CRITICAL — NO NARRATIVE BLENDING:
Do NOT weave together facts from different context sections into a single story. Each section is a separate data source. If a task appears in ACTIVE TASKS, it is NOT complete — even if similar items appear in RECENTLY COMPLETED TASKS.

If the context shows a general timeframe (e.g. "last week", "recently") but not a specific date, use the general timeframe. Never guess an exact date.

Violating these rules is a hallucination. It undermines Danny's trust in you."""

CONTEXT_SECTION_RULES = """CRITICAL — SECTION BOUNDARIES (NEVER CROSS):

The context below is organized into labelled sections. Each section is a SEPARATE data source. You MUST respect these boundaries:

- **ACTIVE TASKS**: Danny's live to-do list. These items NEED ACTION. List them as pending.
- **RECENTLY COMPLETED TASKS**: Already closed. Historical. Do NOT list these as current tasks or claim they "need action".
- **RELEVANT MEMORIES / HINDSIGHT MEMORIES / ON THIS DAY**: Historical records, past notes, temporal patterns. Awareness only — do NOT list as actionable items.
- **TACTICAL MAP / SERENDIPITY / canonical pages**: Graph connections and background intelligence. Do not misrepresent as tasks.
- **ALL OTHER SECTIONS** (emails, whatsapp, resources, calendar, people, practices, projects): Supporting context only.

RULES:
1. If a task is in ACTIVE TASKS, it is NOT complete. Never say it is.
2. If a task is in RECENTLY COMPLETED TASKS, it IS done. Never say it needs action.
3. Never blend ACTIVE TASKS and RECENTLY COMPLETED TASKS into one list."""

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

RESPONSE STRUCTURE:
- Your first sentence answers the question directly.
- If the question asks for an "update" or "status": List ACTIVE TASKS first, then separately note anything from RECENTLY COMPLETED TASKS.
- If you use background context from MEMORIES or CANONICAL sections, clearly signal it as background (e.g. "From past records...").
- Add context (patterns, blockers, urgency) after the answer only if it sharpens the picture.
- No headings like "Part 1" or "Context:".

NEVER:
- Claim a task is "complete" or "done" unless it's in RECENTLY COMPLETED TASKS.
- Merge ACTIVE TASKS and RECENTLY COMPLETED TASKS into one list.
- Present background context from memories as current facts.

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
