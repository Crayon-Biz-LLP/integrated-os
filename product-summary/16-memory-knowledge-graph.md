# 16. Memory System & Knowledge Graph

## The Memory System

### What Gets Stored

The `memories` table stores 5 distinct types of memories, each with its own source lifecycle:

| Memory Type | Source | How It's Created |
|-------------|--------|-----------------|
| `note` | Telegram, Pulse | Messages classified as NOTE → embedded → stored |
| `outcome` | Pulse, API | Task completion triggers `write_outcome_memory()` |
| `archive` | Journal | Google Sheets journal entries via `archive_ingest.py` |
| `reflection` | Pulse | After-action report generated nightly |
| `relationship_note` | Email | FYI emails with `has_memory_value=true` |

### Memory Creation Flow

When a note enters the system (via Telegram or Pulse staging sorter):

1. Content is sent to Gemini Embedding 2 → 768-dim vector
2. Vector + content + metadata inserted to `memories` table
3. If embedding fails → failed_queue with retry
4. Graph edges are NOT created for notes (only for archive memories and tasks)

When a task is completed (outcome memory):

1. Task title + project context combined into a memory
2. Embedded and stored as `memory_type='outcome'`
3. Future briefings can retrieve this via semantic search

### Hybrid Search (Vector + Graph)

The system doesn't rely on vector search alone. It combines multiple signals:

#### Vector Search via RPC
```sql
SELECT * FROM match_memories(
    query_embedding := $embedding_json,
    match_threshold := 0.5,
    match_count := 10
)
```
Returns memories with highest cosine similarity above threshold.

#### Graph Traversal
The `hybrid_search_graph()` function traverses `graph_edges` from relevant entity nodes, following BELONGS_TO and INVOLVES relationships to find connected memories and tasks.

#### Multi-Signal Hindsight Retrieval (`memory.py:104-170`)

The most sophisticated retrieval — runs THREE parallel queries:

```python
# Query 1: Combined task inputs (all new dumps concatenated)
embedding_combined = get_embedding(all_new_inputs)

# Query 2: Per-task queries (top 3 urgent tasks individually)
for task in urgent_tasks[:3]:
    embedding = get_embedding(task['title'])

# Query 3: Entity-seeded queries (from graph traversal)
for entity in graph_context:
    embedding = get_embedding(entity)
```

All queries run concurrently via `asyncio.gather()`. Results are deduplicated by ID, sorted by similarity score, and the top-k returned. The latest timestamp is tracked to detect stale hindsight.

## The Knowledge Graph

### Structure

The graph is stored in two tables:
- `graph_nodes` — vertices with type, label, and rich metadata
- `graph_edges` — directed edges with relationship type and weight

### Node Types

| Type | Created By | Metadata |
|------|-----------|----------|
| `task` | Pulse batch INSERT, Backfill orphan sync | task_id, project_id, source |
| `project` | Pulse project sync, Archive ingest | project_id, org_tag, legacy_id |
| `person` | Archive ingest (`ensure_node`) | people_id, source |
| `practice` | Practice detection, Telegram `/practice` | health_score, frequency, status, variants |
| `mission` | Telegram `/mission` command | status, origin |

### Edge Types

| Relationship | Source → Target | Created By |
|-------------|----------------|-----------|
| `BELONGS_TO` | Task → Project | Pulse batch, Backfill |
| `INVOLVES` | Task → Person | Pulse batch (if name in text) |
| `DEPENDS_ON` | Task → Task | Pulse dependency agent |
| `PRECEDES` / `FOLLOWED_BY` | Practice → Practice | Practice correlation |
| `works_at` / `parent_of` / `belongs_to` / `relates_to` | Person → Entity | Archive ingest `graphify()` |

### Graph Edge Creation for Tasks

When a task is created in the Pulse batch path:
```python
# 1. Create or find task node
node = supabase.table('graph_nodes').insert({
    "label": task_title,
    "type": "task",
    "metadata": {"task_id": task_id, "project_id": project_id}
})

# 2. Find or create project node (should already exist)
project_node = ...

# 3. BELONGS_TO edge
supabase.table('graph_edges').insert({
    "source_node_id": task_node.id,
    "target_node_id": project_node.id,
    "relationship": "BELONGS_TO"
})

# 4. INVOLVES edges for each person mentioned in task text
for person in people:
    if person['name'].lower() in task_title.lower():
        supabase.table('graph_edges').insert({
            "source_node_id": task_node.id,
            "target_node_id": person_node.id,
            "relationship": "INVOLVES"
        })
```

### Visual Exploration

The frontend renders the knowledge graph as an interactive D3.js force-directed visualization with:
- 7 node colors (person, organization, project, mission, task, concept, emotional_state)
- Zoom (0.2x-4x scale)
- Drag with force reheat
- Hover effects (node enlargement, edge highlighting)
- Click to open NodeFlyout detail panel
- 250-tick simulation with auto-stop
