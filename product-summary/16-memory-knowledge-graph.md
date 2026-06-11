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

### Memory Creation & Entity Extraction Flow

When a note or task enters the system (via Telegram or quick processing):

1. Content is sent to Gemini Embedding 2 → 768-dim vector.
2. Vector + content + metadata inserted to `memories` or `tasks` table.
3. **Incremental Entity Extraction:** In real-time, `extract_and_link_entities` runs via Flash Lite. It extracts people, projects, and concepts, automatically inserting `graph_nodes` and creating `MENTIONS` or `RELATED_TO` edges to the source note/task.
4. If embedding fails → failed_queue with retry.

### Memory Decay & Importance Weighting

To prevent stale memories from crowding out relevant recent decisions, retrieval uses the `match_memories_hybrid` RPC. This function scores memories using:

1. **Semantic Similarity:** Cosine distance of the query vector vs memory vector.
2. **Exponential Recency Decay:** Memories age on a 30-day curve `EXP(-days / 15.0)`, giving newer memories a mathematical boost.
3. **Importance Score:** Incorporates the memory's `importance_score` (1-10 scale) directly into the final rank.

### Hybrid Vector+Graph Context (Cross-Referencing)

Rhodey combines multi-signal retrieval through `get_cross_referenced_context()`:

1. **Vector Search:** Fetches semantically similar memories via the hybrid RPC.
2. **Graph Traversal:** Executes `fetch_hybrid_graph_context()` to walk the knowledge graph for related edges.
3. **Cross-Referencing:** The engine automatically scans the vector memory results. If a memory explicitly mentions any entities known to the graph (people, projects), it tags the memory dynamically: `[NOTE] (Links to: Danny, Qhord) ...`.
4. The LLM receives a single, unified context block where textual memories and graph structural relationships are co-presented.

### Temporal Pattern Detection

The `detect_temporal_patterns()` function (`memory.py`) identifies recurring themes by analyzing memories created on the same month-day across different years. Optimizations:
- Uses a targeted `LIKE '%{month_day}%'` filter with a 50-row limit instead of loading and filtering 5000 records in memory
- Runs only on Sundays as part of the `adaptive_briefing_learner` cycle

### Multi-Signal Hindsight Retrieval (`memory.py:104-170`)

The deep-dive retrieval — runs parallel queries:

```python
# Query 1: Combined task inputs (all new dumps concatenated)
# Query 2: Per-task queries (top 3 urgent tasks individually)
# Query 3: Entity-seeded queries (from graph traversal)
```

All queries run concurrently via `asyncio.gather()`. Results are deduplicated by ID, sorted by hybrid score, and the top-k returned.

## The Knowledge Graph

### Structure

The graph is stored in two tables:
- `graph_nodes` — vertices with type, label, and rich metadata
- `graph_edges` — directed edges with relationship type and weight

### Node Types

| Type | Created By | Metadata |
|------|-----------|----------|
| `task` | Pulse batch INSERT, Entity Extractor | task_id, project_id, source |
| `project` | Pulse project sync, Entity Extractor | project_id, org_tag, legacy_id |
| `person` | Archive ingest, Entity Extractor | people_id, source |
| `practice` | Practice detection, Telegram `/practice` | health_score, frequency, status, variants |
| `cluster` | Telegram `/cluster` command | status, origin |
| `concept` | Entity Extractor (Real-time) | source: entity_extractor |
| `emotional_state` | Entity Extractor (Real-time) | source: entity_extractor |
| `resource` | Pulse resource enrichment | url, category, strategic_note |
| `organization` | Archive ingest, Entity Extractor | source, people_id |

### Edge Types

| Relationship | Source → Target | Created By |
|-------------|----------------|-----------|
| `BELONGS_TO` | Task → Project | Pulse batch, Backfill |
| `INVOLVES` | Task → Person | Pulse batch |
| `DEPENDS_ON` | Task → Task | Pulse dependency agent |
| `MENTIONS` | Task/Note → Node | Entity Extractor (Real-time) |
| `RELATES_TO` | Node → Node | Entity Extractor (Real-time), Backfill |
| `INTERESTED_IN` | Person → Node | Backfill (orphaned node edges) |
| `OWNS` | Person → Node | Backfill (orphaned node edges) |
| `WORKS_WITH` | Person → Node | Backfill (orphaned node edges) |
| `KNOWS` | Person → Person | Backfill (orphaned node edges) |
| `FEELS` | Person → emotional_state | Backfill (emotion edges) |
| `WORKS_AT` | Person → Organization | Archive ingest / Extractor |
| `PARENT_OF` | Person → Person | Archive ingest / Extractor |
| `PRACTICES` | Person → Practice | Pulse practice detection |
| `ASSOCIATED_WITH` | Practice → Entity | Pulse practice detection |
| `PRECEDES` | Practice → Practice | Pulse practice detection (temporal) |
| `FOLLOWED_BY` | Practice → Practice | Pulse practice detection (temporal) |

### Graph Health & Backfill Pipeline (CI)

A post-extraction backfill pipeline runs at the end of every CI cycle (`run_backfill()` in `core/skills/backfill_graph.py`). It keeps the graph connected and accurate:

1. **`backfill_orphaned_tasks()`** — Scans for task nodes in the graph that lack a `BELONGS_TO` edge to any project node. Re-creates `BELONGS_TO` edges by looking up parent projects (type in `project`, `cluster`, or `organization`) via metadata. Uses `.limit(1).maybe_single()` to handle duplicate legacy_ids.

2. **`backfill_emotion_edges()`** — Finds `emotional_state` nodes with zero edges and creates `Danny → FEELS → {emotion}` edges, ensuring emotional concepts (e.g., "Suicidal Ideation", "Depression", "Broken") are connected to Danny and visible to retrieval flows.

3. **`backfill_orphaned_node_edges()`** — Finds any non-task node that has NO direct edge to Danny (zero degree or only edges to other non-Danny nodes) and creates a type-appropriate edge:
   - Projects → `OWNS`
   - Concepts → `INTERESTED_IN`
   - People → `KNOWS`
   - Organizations → `WORKS_WITH`
   - Emotional states → `FEELS`
   - Practices → `PRACTICES`
   
   Also deletes garbage "User" nodes. Idempotent — checks for existing Danny edge before inserting.

4. **Dedup** — One-shot dedup at project initialization. Merges duplicate nodes (case-insensitive label match, same type). Repoints affected edges, deletes redundant edges, handles unique constraint conflicts.

### Graph Integrity Safeguards

Three layers protect the knowledge graph from bad data:

1. **Guard A: Orphaned BELONGS_TO edge cleanup (`graph.py`, `backfill_graph.py`)** — When a task's `project_id` changes, any stale `BELONGS_TO` edge for that task (`metadata->>task_id`) is deleted before the new one is inserted. Guarantees exactly one project edge per task, preventing people from ghost-appearing under old projects.

2. **Guard B: Text-anchoring validation (`backfill_graph.py:extract_graph_elements`)** — After LLM extraction, every node label is verified against the source text (case-insensitive substring match). Hallucinated labels (e.g., extracting "Solvstrat" from a text that doesn't mention it) are dropped with an audit warning, along with their edges. "Danny" is always valid for AUTHORED edges.

3. **HITL: Pending approval for high-risk entities (`pending_graph_nodes` table)** — New `person`, `project`, or `organization` nodes are routed to `pending_graph_nodes` with `status: pending`. The Decision Pulse surfaces them via Telegram. You can approve/drop them quickly (`g1 yes`), or use the **NLP Correction Loop** by replying with free-text (e.g., "g1 is actually an organization named Solvstrat"). The OS will interpret the correction and ask for your final confirmation (`yes`) before writing to the graph.

**LLM Extraction Prompt Rule:** The entity extraction prompt includes a CRITICAL RULE: "EVERY node MUST have at least one connecting edge." This prevents the graph from accumulating floating nodes over time. Further, the prompt now includes: "Only extract entities that are explicitly, verbatim stated in the text."

**Definition of orphaned:** Any non-task node that has no direct edge to Danny. This is broader than "zero edges" — nodes with edges to other non-Danny nodes but no Danny edge are reconnected.

### Graph Centrality (Hub Detection)

### Session Memory (Cross-Pulse Continuity)

To maintain context across time without bloated prompt windows, Rhodey implements Session Memory:
1. After generating a Pulse, the agent summarizes the briefing (assigned tasks, key decisions) in 1-2 sentences.
2. This summary is saved to the `core_config` table under `last_pulse_summary`.
3. At the *start* of the next pulse, this summary is retrieved and injected as `SESSION MEMORY`.
This gives the agent "cross-pulse continuity," allowing it to refer to what it recommended in the last session.

### Visual Exploration

The frontend renders the knowledge graph as an interactive D3.js force-directed visualization with:
- 9 node colors (person, organization, project, cluster, task, concept, emotional_state, resource, practice)
- Zoom (0.2x-4x scale)
- Drag with force reheat
- Hover effects (node enlargement, edge highlighting)
- Click to open NodeFlyout detail panel
- 250-tick simulation with auto-stop

**Note:** The frontend API route (`/api/memories`) uses `fetchAllPaginated()` helper to bypass Supabase's 1000-row default limit. All node and edge queries chunk requests in steps of 1000 and concatenate results, ensuring the full graph (~2350+ edges) loads on initial page render.
