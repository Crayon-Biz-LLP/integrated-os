# 10. Task-to-Project Assignment & People Linking

## The 7-Stage Project Assignment Cascade

When the Pulse AI generates a new task with a `project_name`, the engine must resolve it to a `project_id` in the database. Projects are NOT auto-created from task references — if no match is found, the task falls through to the inbox.

The resolution happens through a 7-stage cascading fallback:

### Stage 1: Exact Name Match
```python
ai_target = task.get('project_name', '').lower().strip()
matched = next((p for p in legacy_projects 
               if p.get('name', '').lower() == ai_target), None)
```
Fastest path — direct string match against active project names.

### Stage 2: Keyword Match
```python
for p in legacy_projects:
    kws = [k.lower() for k in (p.get('keywords') or [])]
    if any(kw in ai_target or ai_target in kw for kw in kws):
        matched = p; break
```
Matches against project keyword lists (e.g., project "Qhord" has keywords ["GTM", "product", "pricing"]).

### Stage 3: Description Match
```python
for p in legacy_projects:
    desc = (p.get('description') or '').lower()
    if ai_target in desc:
        matched = p; break
```
Searches project descriptions for the project name.

### Stage 4: Substring Match
```python
matched = next((p for p in legacy_projects 
               if ai_target in p.get('name','').lower() 
               or p.get('name','').lower() in ai_target), None)
```
Checks if either string is a substring of the other.

### Stage 5: Graph Node Match
```python
# Search graph_nodes for projects matching the target
gn_match = next(... from graph_node_projects ...)
```
Queries the knowledge graph for project-type nodes whose labels match.

### Stage 6: Fuzzy Word Match
```python
name_match = next((p for p in legacy_projects 
                  if any(word in p.get('name','').lower() 
                  for word in ai_target.split())), None)
```
Splits the target into words and checks if any word appears in project names.

### Stage 7: Solvstrat/Inbox Fallback
```python
# If work context hints detected, fall back to root Solvstrat project
# Otherwise, use actual_inbox_id (resolved from graph_nodes or legacy_projects)
```
As a last resort, the task goes to Solvstrat (if work-context) or the generic Inbox. A task is NEVEr left without a project — it always lands somewhere.

## People Linking via Knowledge Graph

When a task is created in the Pulse Engine path (Path 2), `write_graph_edges_for_task()` creates:

### Task Graph Node
```python
{"label": task_title, "type": "task", "metadata": 
 {"source": "tasks_table", "task_id": task_id, "project_id": project_id}}
```

### BELONGS_TO Edge
```python
{"source_node_id": task_node_id, "target_node_id": project_node_id,
 "relationship": "BELONGS_TO", "weight": 1.0}
```
Links the task to its project in the knowledge graph.

### INVOLVES Edges (People)
```python
For each person whose name is found in task_title or task_description:
{"source_node_id": task_node_id, "target_node_id": person_node_id,
 "relationship": "INVOLVES", "weight": 1.0}
```
Auto-links tasks to people when their names appear in the task text. This enables queries like "show me all tasks involving Sunju" without manual tagging.

## The Graph Node Gap

**Important**: Graph edges for tasks are ONLY created in the Pulse Engine path (Path 2). The Quick Process inline path (Path 1) does NOT create graph edges. This means:
- Tasks created via inline Telegram processing get Google Calendar + Tasks sync but NO graph edges
- The next `backfill_graph.py` CI run will add the missing edges via `backfill_orphaned_tasks()`
- This is intentional — graph edge creation is async to keep the inline path fast

## People Graph Node Discovery

Person graph nodes are created primarily through:
1. **Archive ingest**: `ensure_node()` creates nodes for known names from journal text
2. **Practice detection**: Creates person nodes for entity tracking
3. **Backfill graph**: `sync_person_nodes_to_people_table()` links person graph nodes to the `people` table

The Pulse Engine does NOT create person graph nodes when inserting people into the `people` table. This is a known architectural constraint — person nodes rely on the backfill step to be connected.
