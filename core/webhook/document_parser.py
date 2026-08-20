"""Document Intelligence — parse documents into structured breakdowns.

When a document is uploaded via the app, this module extracts structured
information (action items, decisions, dates, key facts) so the user can
review and batch-confirm before creating tasks/events/notes.

For simple documents (reference notes, single-purpose), the system falls
back to the classic classify → route flow.
"""

from typing import Optional
from core.lib.suggestion_extractor import extract_suggestions

async def parse_document(extracted_text: str) -> Optional[dict]:
    """Parse extracted document text into structured breakdown.
    
    Delegates to the unified suggestion extractor.
    
    Returns:
        {
            "document_type": str,
            "summary": str,
            "suggested_actions": list[dict],
            "suggested_entities": list[dict]
        }
        or None on failure.
    """
    return await extract_suggestions(extracted_text)
