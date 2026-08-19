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
from core.llm.constants import CLASSIFICATION_MODEL

logger = logging.getLogger(__name__)

# ── Structured output schema ──────────────────────────────────────
# Uses CLASSIFICATION_MODEL (gemini-3.5-flash-lite) for speed — this
# is a simple extraction task, not deep synthesis.

DOCUMENT_PARSE_PROMPT = """\
Analyze this document and extract structured information.

DOCUMENT:
{text}

Return ONLY valid JSON:
{{
  "document_type": "<invoice|meeting_minutes|contract|report|receipt|proposal|other>",
  "summary": "<2-3 sentence summary>",
  "suggested_actions": [
    {{
      "type": "<task|event|note>",
      "title": "<action title, under 60 chars>",
      "owner": "<person name if mentioned, else null>",
      "deadline": "<ISO date if mentioned, else null>",
      "org_hint": "<org name if mentioned, else null>",
      "description": "<1-2 sentence context>"
    }}
  ]
}}

RULES:
- suggested_actions = actionable items only, not passive observations
- If no actions needed, return empty suggested_actions
- Deadlines must be absolute dates (2025-08-25, not 'next Friday')
"""


async def parse_document(extracted_text: str) -> Optional[dict]:
    """Parse extracted document text into structured breakdown.
    
    Returns:
        {
            "document_type": str,
            "summary": str,
            "suggested_actions": list[dict]
        }
        or None on failure.
    """
    if not extracted_text or not extracted_text.strip():
        return None

    prompt = DOCUMENT_PARSE_PROMPT.format(
        text=extracted_text[:4000]  # Cap at 4K — flash-lite is fast but needs tight input
    )

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
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

        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"Document parser: invalid JSON from LLM: {e}")
        return None
    except Exception as e:
        logger.error(f"Document parser error: {e}")
        return None
