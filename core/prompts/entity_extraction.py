"""DEPRECATED: Entity extraction prompt.

Entity detection is now done by core/lib/entity_detector.py (deterministic, no LLM).
This file is kept for backward compatibility. It re-exports the relationship-only
prompt from core.prompts.relationship.

OLD behavior (removed):
  - SHARED_EXTRACTION_PROMPT with entity type guidance, ✓/✗ examples,
    anti-patterns, text-anchoring rules, canonical name rules
  - All entity type classification was done by the LLM based on examples
  - 80+ lines of complex instructions that introduced bias

NEW behavior:
  - Entities are detected by deterministic code (no LLM)
  - LLM only extracts relationships between already-detected entities
  - No examples, no bias, no prompt drift possible
"""

import warnings

from core.prompts.relationship import RELATIONSHIP_EXTRACTION_PROMPT

# Backward compatibility alias
SHARED_EXTRACTION_PROMPT = RELATIONSHIP_EXTRACTION_PROMPT

warnings.warn(
    "SHARED_EXTRACTION_PROMPT is deprecated. Entity detection uses "
    "core.lib.entity_detector.detect_entities(). "
    "Use core.prompts.relationship.RELATIONSHIP_EXTRACTION_PROMPT for relationships.",
    DeprecationWarning, stacklevel=2
)
