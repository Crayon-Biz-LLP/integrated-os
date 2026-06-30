from typing import Optional
from core.prompts.guards import inject_guards

def build_interrogate_brain_prompt(
    now_str: str,
    sources_str: str,
    context_str: str,
    conversation_history: str,
    query: str
) -> str:
    guards = inject_guards("query")
    return f"""{guards}

CURRENT TIME: {now_str}

Danny is asking a question. You have access to his: {sources_str}.

CRITICAL OUTPUT FORMAT - YOU MUST RETURN ONLY VALID JSON:
{{
  "answer_type": "memory_match|status_only",
  "user_facing_summary": "The answer to the user's question. Use bullet points and markdown if listing tasks/events. If an event is marked [PAST], explicitly mention it. Summarize context before lists if relevant.",
  "claimed_actions": [],
  "needs_execution": false
}}

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
