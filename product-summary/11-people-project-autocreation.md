# 11. People & Project Auto-Creation

## Project Auto-Creation (AI-Gated)

Projects are NOT auto-created when a task references a non-existent project. They are only created when the AI explicitly adds them to the `new_projects` array in its JSON output.

### The AI Prompt Rule

The Pulse prompt strictly constrains project creation by domain:

> **SOLVSTRAT**: Auto-create new projects for completely unknown client names mentioned (e.g., a company hiring Solvstrat for tech work). Set `organization_name: "SOLVSTRAT"`, `parent_project_name: "Solvstrat"`.

> **OTHER DOMAINS** (QHORD, ASHRAYA, PERSONAL, CRAYON): ONLY create a new project if Danny explicitly says "create a project", "start a new project", or gives a clear commanding instruction. Otherwise, route the work as a task under the existing parent project. Do NOT auto-create projects for one-off tasks or casual mentions.

This means client engagements like "Equisoft" or "Armour Cyber" are auto-created as projects under Solvstrat. But one-off tasks like "Trust account ReKYC" or "Follow up with the bank" are NOT created as projects — they go as tasks under their parent domain (Ashraya).

### The Processing Pipeline

When the AI outputs `new_projects`, the write phase (engine.py:1039-1132) processes each entry:

```python
for p in ai_data['new_projects']:
    name = p.get('name', '').strip()
    
    # 1. VALIDATE ORG TAG
    tag = p.get('organization_name', 'SOLVSTRAT')
    if tag not in ['SOLVSTRAT', 'QHORD', 'PERSONAL', 'CRAYON', 'ASHRAYA']:
        continue
    
    # 2. DEDUP: fuzzy substring match against graph_nodes + projects table
    already_exists = any(
        name.lower() in existing_name.lower() or existing_name.lower() in name.lower()
        for existing_name in all_existing_project_names
    )
    
    # 3. REQUIRE DESCRIPTION
    if not p.get('description'):
        continue
    
    # 4. RESOLVE PARENT PROJECT
    parent_id = None
    if p.get('parent_project_name'):
        parent = match against legacy_projects
        parent_id = parent['id'] if parent else None
    
    # 5. INSERT
    supabase.table('projects').insert({...}).execute()
    
    # 6. CREATE/UPGRADE GRAPH NODE
    checks if matching graph node exists:
        - If exists AND not type 'project': upgrade to 'project'
        - If exists AND is 'project': update metadata
        - If none: create new graph_node (type='project', metadata with project_id, organization_name)
```

### Deduplication Strategy

Fuzzy substring matching against two sources:
1. `graph_nodes` (type='project') — covers projects created via other paths
2. `legacy_projects` — projects table

A project named "Qhord" would match against existing "Qhord GTM Strategy" or "Qhord Sales" — preventing duplicates.

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

## The Two-Way Graph-Table Bridge

`backfill_graph.py` runs every Pulse and syncs in both directions:

### Graph → Table
- Person graph nodes without `people_id` → matched or inserted into `people` table
- Project graph nodes without `legacy_id` → matched against `projects` table, stamped with ID

### Table → Graph
- Tasks without graph node entries → `backfill_orphaned_tasks()` creates nodes + WORKS_ON + DISCUSSED_WITH edges
- Uses `upsert` with `on_conflict="label"` to avoid duplicates

### Guard Integration

The auto-creation paths now interact with guards:

- **Project creation (Pulse AI Batch):** When the AI creates a new project via `new_projects`, it inserts into both `projects` table AND `graph_nodes`. This path bypasses Guard 2 because it originates from the AI's structured output (not LLM extraction). The `projects` table entry ensures future extractions of the same project name will be grounded.
- **Person creation (all 4 paths):** When a new person is created from any path, they enter the `people` table. Future extractions matching this name will be grounded by Guard 3 (`has_structural_anchor()`) and routed to pending with `status='pending'` instead of `status='flagged'`.
- **Backfill orphaned tasks:** Creates task nodes directly in `graph_nodes` (tasks are structural entities, not extraction entities). Uses `upsert` to avoid duplicates. Transitional edges tagged with `source='transitional'` for the task node cleanup script to manage.

This ensures the knowledge graph and relational tables stay consistent even when one path creates an entity without updating the other.
