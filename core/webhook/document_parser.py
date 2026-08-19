"""Document Intelligence — parse documents into structured breakdowns.

When a document is uploaded via the app, this module extracts structured
information (action items, decisions, dates, key facts) so the user can
review and batch-confirm before creating tasks/events/notes.

For simple documents (reference notes, single-purpose), the system falls
back to the classic classify → route flow.
"""

import json
import logging
from typing import Optional

from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import SYNTHESIS_MODEL

logger = logging.getLogger(__name__)

# ── Structured output schema ──────────────────────────────────────

DOCUMENT_PARSE_PROMPT = """\
You are an executive assistant analyzing a document. Extract structured information.

DOCUMENT TEXT:
{extracted_text}

Your task:
1. Identify the document type
2. Write a 2-3 sentence summary
3. Extract key facts (type-specific)
4. List suggested actions (tasks, events, notes to create)

Return ONLY valid JSON (no markdown, no explanation):
{{
  "document_type": "<invoice|meeting_minutes|contract|email_thread|report|receipt|proposal|policy|resume|other>",
  "complex": <true if multiple action items, decisions, or stakeholders>,
  "summary": "<2-3 sentence summary of what this document is about>",
  "key_facts": {{
    // Type-specific fields. Examples:
    // invoice: "vendor", "amount", "due_date", "line_items"
    // meeting_minutes: "attendees", "decisions", "action_items_summary"
    // contract: "parties", "term", "expiry", "key_terms"
    // report: "period", "key_findings", "recommendations"
    // email_thread: "from", "subject", "decisions", "action_items_summary"
    // receipt: "vendor", "amount", "date", "items"
    // other: "description", "relevant_dates"
  }},
  "suggested_actions": [
    {{
      "type": "<task|event|note>",
      "title": "<concise action title>",
      "owner": "<person name if mentioned, else null>",
      "deadline": "<ISO date if mentioned, else null>",
      "date": "<ISO date for events, else null>",
      "org_hint": "<organization name if mentioned, else null>",
      "description": "<1-2 sentence context for the action>"
    }}
  ]
}}

RULES:
- "complex" = true if the document has 2+ action items, decisions, or involves multiple people
- "complex" = false for simple reference documents (single invoice, receipt, brief note)
- suggested_actions should be actionable items, not passive observations
- If the document is just a reference (no actions needed), return empty suggested_actions
- Keep titles concise (under 60 chars)
- Deadlines should be absolute dates, not relative ("2025-08-25", not "next Friday")
"""


async def parse_document(extracted_text: str) -> Optional[dict]:
    """Parse extracted document text into structured breakdown.
    
    Returns:
        {
            "document_type": str,
            "complex": bool,
            "summary": str,
            "key_facts": dict,
            "suggested_actions": list[dict]
        }
        or None on failure.
    """
    if not extracted_text or not extracted_text.strip():
        return None

    prompt = DOCUMENT_PARSE_PROMPT.format(
        extracted_text=extracted_text[:8000]  # Cap at 8K chars to stay within token limits
    )

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.CLASSIFY,
            primary_model=SYNTHESIS_MODEL,
        )
        response = response.text if response and response.text else None

        if not response:
            logger.warning("Document parser: empty LLM response")
            return None

        # Parse JSON from response (handle markdown code blocks)
        text = response.strip()
        if text.startswith("```"):
            # Remove markdown code fence
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        
        parsed = json.loads(text)

        # Validate required fields
        if "document_type" not in parsed or "summary" not in parsed:
            logger.warning("Document parser: missing required fields")
            return None

        # Ensure suggested_actions is a list
        if "suggested_actions" not in parsed:
            parsed["suggested_actions"] = []

        # Ensure key_facts is a dict
        if "key_facts" not in parsed or not isinstance(parsed["key_facts"], dict):
            parsed["key_facts"] = {}

        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"Document parser: invalid JSON from LLM: {e}")
        return None
    except Exception as e:
        logger.error(f"Document parser error: {e}")
        return None
