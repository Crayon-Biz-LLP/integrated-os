# 11. People & Project Auto-Creation

## Project Auto-Creation (AI-Gated — Decommissioned)

**The `projects` table and its write pipeline were decommissioned in Phase 2 (July 2026).** Projects are no longer created via a `new_projects` JSON array. Instead, project-type graph nodes are created on-demand through entity linking and the Pulse AI prompt.

### Current Behavior

Projects (work streams like "Equisoft", "Armour Cyber") are now represented as `type='project'` graph nodes under parent organizations. They are created through two paths:

1. **Entity extraction**: When the entity linker finds a new project name in task/note content, it creates a `type='project'` graph node with a `BELONGS_TO` edge to the parent org node.
2. **Backfill sync**: `backfill_graph.py` discovers project-type nodes from existing memory patterns and syncs them to the graph.

### The AI Prompt Rule

The Pulse prompt constrains project creation by domain:

> **SOLVSTRAT**: Auto-create project graph nodes for completely unknown client names mentioned. Set `organization_name: "SOLVSTRAT"`.

> **OTHER DOMAINS** (QHORD, ASHRAYA, PERSONAL, CRAYON): ONLY create a project graph node if Danny explicitly instructs. Otherwise, route the work as a task under the existing parent org.

Client engagements are auto-created as project nodes under Solvstrat. One-off tasks go under their parent org without a project node.

### Deduplication Strategy

Project graph node dedup uses fuzzy substring matching against `graph_nodes` (type='project') only — the `projects` table is no longer consulted.

## People Auto-Creation (4 Paths)

People enter the system through 4 distinct paths, each with blocklist protection and dual-level dedup:

### Path 1: Pulse AI Batch
**File**: `engine.py:1161`
AI detects a new person mentioned and adds to `new_people`. Processing:
1. Blocklist check via `is_blocklisted_person()` (16 generic terms)
2. Raw name dedup (lowercase + strip)
3. Normalized name dedup (parentheticals removed, titles stripped)
4. Non-person graph node dedup (prevents creating "Danny" if "Danny" project node exists)
5. Batch INSERT with `source='pulse'`

### Path 2: Gmail Sender
**File**: `email_ingest.py:57` — `add_person_from_email()`
When an email from a human sender is classified as fyi or actionable:
1. Blocklist check
2. Fetch all existing people, build name→id map (raw + normalized)
3. Match against both raw and normalized names
4. INSERT if no match found, with role = None, strategic_weight = 5

### Path 3: Gmail Linked Person
**File**: `email_ingest.py:414`
When Gemini classifies an email as actionable and identifies a `linked_person_name`:
1. Blocklist check
2. Attempt ilike lookup against people table (fuzzy)
3. If not found: INSERT with `source='email_ingest'`

### Path 4: Backfill Graph Sync
**File**: `backfill_graph.py:988`
When `sync_person_nodes_to_people_table()` finds person graph nodes not yet linked to `people` table:
1. Blocklist check
2. Raw + normalized name dedup against existing people
3. If no match: INSERT with `source='backfill_graph'`

### The Blocklist

16 generic terms that should never become people entries:
```python
{"wife", "parents", "sister's family", "customer", "employee",
 "finance manager", "kids", "author", "narrator", "user",
 "mother", "aunt", "uncle", ...}
```

### Name Normalization

Strips titles + parentheticals to enable cross-path dedup:
```python
"Pastor John (PhD)" → "john"
"Dr. Sarah Smith" → "sarah smith"
```

## The Three-Way Graph-Table Bridge

`backfill_graph.py` runs every Pulse and syncs in **three directions** — people, organizations, and projects now each have their own dedicated sync function:

### Graph → Table
- **`sync_person_nodes_to_people_table()`:** Person graph nodes without `graph_node_id` link → matched or inserted into `people` table. **Skips orphaned `[DELETED]`/`[CHANGED TO ORGANIZATION]`/`[MERGED INTO` role flags** to prevent recreating cleaned-up records.
- Project graph nodes without `db_record_id` → matched against `projects` table, stamped with ID.

### Table → Graph (People)
- **`sync_people_to_graph_nodes()`:** Iterates all `people` rows. For each:
  1. Checks if a `type='person'` graph_node already exists via `db_record_id`.
  2. If not, resolves the canonical label via `resolve_canonical_label()` (uses `normalize_label()` under the hood).
  3. For **orphaned entries** (role contains `[DELETED]`, `[CHANGED TO ORGANIZATION]`, `[MERGED INTO`): skips them entirely — no graph node created.
  4. Creates `type='person'` graph node with `db_record_id = people.id`, `metadata.source = 'sync:people'`.
  5. **Verification assertion:** Post-sync count of person-type graph nodes must be within ±5 of total `people` rows (excluding orphans), otherwise raises `AssertionError`.

### Table → Graph (Organizations)
- **`sync_organizations_to_graph_nodes()`:** Iterates all `organizations` rows. For each:
  1. Checks for existing graph node via `db_record_id`.
  2. If existing node has **wrong type** (e.g. `person` instead of `organization`): **deletes old node** (cascades graph_edges), then creates fresh `type='organization'` node. This fixes the historical bug where entity extraction created person-type nodes for organizational entities.
  3. If no existing node: creates `type='organization'` with `db_record_id`, `metadata.source = 'sync:organizations'`.
  4. **Verification:** Post-sync count of organization-type graph nodes with `db_record_id` must be ≥ total `organizations` rows minus known label-collision exceptions (same name used as both org and project).

### Table → Graph (Projects) — Decommissioned

**`sync_projects_to_graph_nodes()` was removed in Phase 2.** The `projects` table is no longer the source of truth for project graph nodes. Project-type nodes are now created through entity extraction or the backfill's pattern-discovery path. Any remaining `projects` table rows exist only for backward compatibility in test fixtures.

### Label Resolution Logic (`core/lib/graph_rules.py`)

The canonical label resolver was hardened to prevent **reappearing deleted nodes** — the root cause of the original bug:

**`resolve_canonical_label(label)`:**
1. Normalizes input via `normalize_label()` (lowercase, strip).
2. Checks `pending_graph_nodes` for the label with `status='rejected'` — if found, returns `is_rejected=True` (never recreate).
3. Checks `people` table for the label:
   - If found AND `people.role` contains `[DELETED]` → returns `is_rejected=True`
   - If found AND `people.role` contains `[CHANGED TO ORGANIZATION]` → returns `is_rejected=True`
   - If found AND `people.role` contains `[MERGED INTO` → returns `is_rejected=True`
   - Otherwise → returns `('person', id, matched_label, is_rejected=False)`
4. Checks `organizations` table for the label → returns `('organization', id, ...)` on match.
5. Checks `graph_nodes` for existing non-person nodes → returns type + id.
6. Falls through to `pending_graph_nodes` for any match.

**`normalize_label(label)`:**
- Shared helper used by all three sync functions.
- Lowercases and strips whitespace.
- Used to build the `label_to_node` dict for O(1) lookup during sync.

### Deletion Provenance (Why Deleted Nodes Stay Deleted)

The system prevents reappearing deleted graph nodes via three independent layers:
1. **Hard blocklist:** `pending_graph_nodes` with `status='rejected'` — `resolve_canonical_label()` checks this first.
2. **Soft blocklist:** `people.role` suffix markers — `[DELETED]`, `[CHANGED TO ORGANIZATION]`, `[MERGED INTO` — the sync functions skip these rows entirely.
3. **Sync exclusion:** `sync_people_to_graph_nodes()` skips orphaned role entries before attempting label resolution. Combined with the exact guard pattern in `resolve_canonical_label()`, even if a new memory re-extracts the same label, it'll hit the blocklist and won't recreate the node.

### Current Coverage (July 2026)

| Domain Table | Rows | Matching graph_nodes (via db_record_id) | Gap Reason |
|---|---|---|---|
| `people` | 135 | 105 person-type nodes | 30 orphans (marked `[DELETED]`) |
| `organizations` | 33 | 29 org-type nodes | 4 label collisions (same name used for both org and project-type nodes) |
| `graph_nodes` (project-type) | ~22 | N/A | Created via entity extraction and backfill — not synchronized to a relational table |

**Label collisions** (same name used for both org + project-type nodes): Ashraya, Solvstrat, Qhord, PERSONAL. Both node types coexist as different graph nodes sharing the same label. This is an accepted data model limitation enforced by the `unique_label` constraint — when a collision occurs, the org sync creates the org-type node and the project-type node is manually resolved.

### Guard Integration

The auto-creation paths now interact with guards:

- **Project creation (entity extraction):** When the entity linker discovers a new project name, it creates a `type='project'` graph node directly — no `projects` table insert. The node flows through the standard HITL pending approval path (`pending_graph_nodes`). Entity grounding guards (`has_structural_anchor()`) ensure only clearly identifiable project references trigger creation.
- **Person creation (all 4 paths):** When a new person is created from any path, they enter the `people` table. Future extractions matching this name will be grounded by Guard 3 (`has_structural_anchor()`) and routed to pending with `status='pending'` instead of `status='flagged'`.
- **Backfill orphaned tasks:** Creates task nodes directly in `graph_nodes` (tasks are structural entities, not extraction entities). Uses `upsert` to avoid duplicates. Transitional edges tagged with `source='transitional'` for the task node cleanup script to manage.
- **Orphaned role markers:** When a people row is manually marked `[DELETED]` or auto-marked `[CHANGED TO ORGANIZATION]`, both the sync function AND `resolve_canonical_label()` refuse to recreate its graph node. The only way to revive a deleted person is to clear the role suffix manually.

This ensures the knowledge graph and relational tables stay consistent even when one path creates an entity without updating the other.
