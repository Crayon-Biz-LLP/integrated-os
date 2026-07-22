"""Relationship extraction prompt — single source of truth.

Entity detection is done by deterministic code (core/lib/entity_detector.py).
This prompt is ONLY for extracting relationships between already-detected entities.
No entity type guidance, no examples, no anti-patterns, no ✓/✗ lists.
"""

RELATIONSHIP_EXTRACTION_PROMPT = """
You are given a text and a list of entities already detected in it.
Extract the RELATIONSHIPS between these entities.

Text: "{text}"

Detected entities:
{entities}

Return a JSON array of objects with:
  source: entity label (MUST be one of the detected entities above)
  target: entity label (MUST be one of the detected entities above)
  relationship: a short description of how they relate (e.g., WORKS_AT, WORKS_ON,
    BELONGS_TO, KNOWS, CLIENT_OF, MET_WITH, INVOLVES, FEELS, MENTORED, INTRODUCED)

Rules:
- Only extract relationships explicitly stated or strongly implied in the text.
- Both source and target must be from the provided entity list. Do NOT invent new entities.
- If no relationships exist between the given entities, return an empty array [].
- Do NOT include generic, weak, or inferred relationships unless the text clearly
  states or strongly implies them.
"""
