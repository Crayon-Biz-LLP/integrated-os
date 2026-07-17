"""Shared entity extraction prompt — single source of truth.

Both entity_extractor.py (real-time) and backfill_graph.py (batch) import
this prompt. Any fix or improvement to extraction rules applies to both
paths automatically, preventing prompt drift.
"""

SHARED_EXTRACTION_PROMPT = """
Extract knowledge graph elements from this text.

Return a JSON object with:
- "nodes": array of objects with {"label": string, "type": "person"|"organization"|"project"|"place"|"event"|"animal"|"emotional_state"|"task"}
- "edges": array of objects with {"source": string, "target": string, "relationship": string}

RULES:
- Only extract explicitly mentioned entities. Every "source" and "target" label MUST appear verbatim in the text.
- Keep labels concise (e.g. "Danny", "Qhord").
- COMMON MISTAKES TO AVOID:
  - Use canonical names for known entities: "Danny" (not "I", "me", "user"), "Mother" (not "Amma", "amma").
  - Do not extract pronouns or generic terms ("he", "the project", "loops") as nodes.
- RELATIONAL EDGES (extract these first, from explicit statements):
  - Person → Organization: extract WORKS_AT for employer affiliations. Examples: "Marcus from Ashraya" -> WORKS_AT, "talked to Binu at Equisoft" -> WORKS_AT
  - Person → Project: extract WORKS_ON for work relationships
  - Project → Organization: extract BELONGS_TO when a project is described as belonging to or being for an org
- When a project's organization_id already exists in the database, use that FK over text inference. Text fills gaps, not overrides.
- Skip edges where you cannot confidently determine the relationship type.
- AVOID COMBINING ENTITIES: Never combine an organization and a project into a single label. E.g. "Armour Cyber AI Gateway" must be split into "Armour Cyber" (organization) and "AI Gateway" (project).
- TYPE GUIDANCE:
  - "place": A physical location, venue, or geographic area (e.g. "St. Mary's Church", "Kakkanad office").
  - "event": A scheduled or past occurrence with a time/date (e.g. "Sunday service", "team standup").
  - "animal": Named or referenced pets, animals (e.g. "Max", "the stray cat").
  - "emotional_state": A feeling, mood, or emotional condition (e.g. "stressed", "excited", "overwhelmed").
  - "project": A named initiative with a defined goal and stakeholders.
    ✓ QHORD, Ashraya, Solvstrat, Rhodey OS
    ✗ "Church cash rotation incident" (event), "New Habit" (intention), "Journaling tool" (concept), "Call Marcus" (task)
    If it doesn't have a formal name someone would use to refer to an ongoing initiative — skip it.
- TEXT-ANCHORING: Every extracted label MUST appear verbatim (case-insensitive) in the source text.
  If a label doesn't appear in the text, do NOT include it even if you can infer it from context.
- CONSISTENCY: EVERY label referenced in an edge's "source" or "target" MUST also appear in the "nodes" array with its type.
- If no clear entities/relationships, return empty arrays.
- Normalize person names to First Last if obvious.
"""
