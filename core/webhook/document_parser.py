"""Document Intelligence — parse documents into structured breakdowns.

When a document is uploaded via the app, this module extracts structured
information (action items, decisions, dates, key facts) so the user can
review and batch-confirm before creating tasks/events/notes.
"""

from typing import Optional
from core.lib.suggestion_extractor import extract_suggestions
from core.lib.entity_context import extract_context_from_source

async def parse_document(extracted_text: str) -> Optional[dict]:
    """Parse extracted document text into structured breakdown.
    
    Delegates to the unified suggestion extractor and context extractor.
    """
    actions, breakdown = await extract_suggestions(extracted_text, intent="NOTE")
    if not breakdown:
        return None
        
    ctx = await extract_context_from_source(extracted_text, timing="card")
    breakdown["suggested_entities"] = ctx.detected_entities
    breakdown["entity_context"] = ctx.to_dict()
    
    return breakdown
