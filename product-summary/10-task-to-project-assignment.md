# 10. Task-to-Organization Assignment & People Linking

> ## ⚠️ SUPERSEDED — decommissioned feature (kept for history)
> The project/people assignment features described here are **decommissioned**
> (Jul 2026): `organizations`/`people` tables dropped (db/75), the projects
> feature is dormant (X1 decision, 2026-08-15 — see `05-database-schema.md`),
> and routing is now entity/tenant-based. Kept as a historical record.

## The Organization Assignment Cascade

When the Pulse AI generates a new task with an `organization_name`, the engine resolves it via the Action Planner (`core/actions/planner.py`). The `projects` table was decommissioned — task routing now uses the `organizations` table with graph-node fallback.

The resolution flow:

### Step 1: Planner extracts org context
```python
candidate_words = set()
if c.get("organization_name"):
    candidate_words.extend(c["organization_name"].lower().split())
```
The planner prompt instructs Gemini to classify tasks under known orgs (SOLVSTRAT, QHORD, ASHRAYA, PERSONAL, CRAYON). Unknown entity names suggested by the user for task context are treated as project/client names under the parent org.

### Step 2: Org ID resolution (`tools.py`)
```python
org_res = supabase.table('organizations').select('id').ilike('name', organization_name).limit(1).execute()
```
The `organizations` table is the single source of truth. `organization_id` is stamped on the task row. No `project_id` column exists on tasks anymore.

### Step 3: Graph node linking
Task nodes get `BELONGS_TO` edges to the resolved organization's graph node. Project-type graph nodes still exist for client projects under an org but are discovered via the graph, not via a separate `projects` table.

## People Linking via Knowledge Graph

When a task is created in the Pulse Engine path (Path 2), `write_graph_edges_for_task()` creates:

### Task Graph Node
```python
{"label": task_title, "type": "task", "metadata": 
 {"source": "tasks_table", "task_id": task_id, "organization_id": org_id}}
```

### BELONGS_TO Edge
```python
{"source_node_id": task_node_id, "target_node_id": org_node_id,
 "relationship": "BELONGS_TO", "weight": 1.0}
```
Links the task to its organization's graph node in the knowledge graph. Project-level BELONGS_TO edges are created when the entity linker resolves a specific project name under the org.

### INVOLVES Edges (People)
```python
For each person whose name is found in task_title or task_description:
{"source_node_id": task_node_id, "target_node_id": person_node_id,
 "relationship": "INVOLVES", "weight": 1.0}
```
Auto-links tasks to people when their names appear in the task text. This enables queries like "show me all tasks involving Sunju" without manual tagging.

## The Graph Node Gap

**Important**: Graph edges for tasks are ONLY created in the Pulse Engine path. Tasks created via Quick Command or inline Telegram processing get sync but NO graph edges until the next backfill run.
- The Action Planner path always creates graph edges
- `backfill_graph.py` adds missing edges via `backfill_orphaned_tasks()` on each Pulse cycle

## People Graph Node Discovery

Person graph nodes are created primarily through:
1. **Archive ingest**: `ensure_node()` creates nodes for known names from journal text
2. **Practice detection**: Creates person nodes for entity tracking
3. **Backfill graph**: `sync_person_nodes_to_people_table()` links person graph nodes to the `people` table

The Pulse Engine does NOT create person graph nodes when inserting people into the `people` table. This is a known architectural constraint — person nodes rely on the backfill step to be connected.
