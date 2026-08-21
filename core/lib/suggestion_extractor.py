"""Unified extraction: extract tasks and entities from user-initiated content.

This module provides a single LLM pass to extract both actionable tasks
and graph entities (people, orgs, projects) from content the user provides
(documents or chat messages).
"""

import json
import logging
from typing import Optional

from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import CLASSIFICATION_MODEL

logger = logging.getLogger(__name__)

SUGGESTION_EXTRACTION_PROMPT = """\
Analyze this content and extract structured information, including actionable tasks.

CONTENT:
{text}

Return ONLY valid JSON:
{{
  "document_type": "<invoice|meeting_minutes|contract|report|receipt|proposal|message|other>",
  "summary": "<2-3 sentence summary>",
  "suggested_actions": [
    {{
      "type": "<task|event|note>",
      "title": "<action title, under 60 chars>",
      "owner": "<person name if mentioned, else null>",
      "date": "<ISO date for events, else null>",
      "deadline": "<ISO date for tasks, else null>",
      "description": "<1-2 sentence context>"
    }}
  ]
}}

RULES:
- suggested_actions = actionable items only, not passive observations
- If no actions needed, return empty suggested_actions
- Deadlines must be absolute dates (2025-08-25, not 'next Friday')
"""

async def extract_suggestions(content: str) -> Optional[dict]:
    """Parse content into a structured breakdown of actions and entities.
    
    Returns:
        {
            "document_type": str,
            "summary": str,
            "suggested_actions": list[dict],
            "suggested_entities": list[dict]
        }
        or None on failure.
    """
    if not content or not content.strip():
        return None

    # Cap at 4K for speed with flash-lite
    prompt = SUGGESTION_EXTRACTION_PROMPT.format(text=content[:4000])

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
        )
        response_text = response.text if response and response.text else None

        if not response_text:
            logger.warning("Suggestion extractor: empty LLM response")
            return None

        text = response_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        
        parsed = json.loads(text)

        if "document_type" not in parsed or "summary" not in parsed:
            logger.warning("Suggestion extractor: missing required fields")
            return None

        if "suggested_actions" not in parsed:
            parsed["suggested_actions"] = []
            
        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"Suggestion extractor: invalid JSON from LLM: {e}")
        return None
    except Exception as e:
        logger.error(f"Suggestion extractor error: {e}")
        return None
