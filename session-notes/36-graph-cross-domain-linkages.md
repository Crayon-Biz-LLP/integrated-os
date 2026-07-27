# 36. Graph Cross-Domain Linkages & Multi-Layered Extraction

**Date:** Jul 12, 2026
**Status:** Implemented & Backfilled

## Problem
Domain tables (`people`, `organizations`, `projects`) were drifting from the Knowledge Graph due to incomplete bidirectional foreign keys (`graph_node_id` missing on domain rows, `db_record_id` missing on graph_nodes). 
Additionally, cross-domain edges (`WORKS_AT` for person→org, `BELONGS_TO` for project→org) were vastly under-represented (e.g., only 21 WORKS_AT edges existed across 200+ people) because LLM extraction sporadically missed implicit affiliations ("Marcus from Ashraya"), resulting in isolated islands in the graph visualization.

## Architecture

### 1. Multi-Layered Edge Extraction
To ensure cross-domain relationships are captured without introducing deterministic noise, the system uses a 4-layer defense-in-depth approach:

- **Layer 1: Prompt Hardening (Extraction Time)**: `entity_extractor.py` provides explicit examples of informal language (e.g., `"Marcus from Ashraya" -> WORKS_AT`) directly in the Gemini Flash Lite prompt.
- **Layer 2: Pattern Backstop (Extraction Time)**: After LLM extraction (`insert_extracted_entities`), a deterministic regex scans the source text for linguistic affiliation patterns (e.g., `X from Y`, `X works at Y`) specifically for *newly extracted* (pending) persons and orgs that co-occur in the same text.
- **Layer 3: Post-Creation Scan (Approval Time)**: When a Person or Project is approved via Decisions UI, `create_graph_node_with_db_record` runs a conservative exact/alias/substring (≥6 char) match against known organizations on the source snippet, staging a `WORKS_AT` or `BELONGS_TO` pending edge if matched.
- **Layer 4: Periodic Sync (Maintenance Time)**: `backfill_graph.py` periodically scans the curated `people.organization_name` column and pushes pending `WORKS_AT` edges for legacy or manually entered data.

### 2. Bidirectional FK Integrity
- **Graph to Domain**: `graph_nodes.db_record_id` points to the primary key of the corresponding table (`people`, `organizations`, `projects`).
- **Domain to Graph**: `people.graph_node_id` and `organizations.graph_node_id` point back to the canonical graph node. 
- *Note:* `projects`, `tasks`, and `memories` intentionally do not have `graph_node_id` columns; the graph navigates to them via `db_record_id`, which is sufficient for retrieval and visualization.

### 3. Schema Constraints
- `VALID_EDGE_MATRIX` explicitly enforces `BELONGS_TO` as the canonical direction for `project→organization` and `task→organization`.
- The `OWNS` edge type has been completely removed from the writable matrix (it is only preserved as a read-side alias or specific root-node relationship, e.g., Danny OWNS project).

## Backfill Execution
A one-time backfill was executed to bridge existing data:
1. **FK Sync**: 41 legacy canonical `people` rows were mapped and linked to their `graph_nodes` via exact name matching.
2. **Structural Auto-Approval**: 28 structural pending edges (15 `project→org`, 13 `task→project`) generated from DB foreign keys were auto-approved, successfully resolving validation constraints (7 auto-rejected due to project→project mapping).
3. **Legacy Affiliations**: `WORKS_AT` edges were generated for people possessing valid non-self-referencing `organization_name` values (e.g., Marcus Durai → Ashraya).
