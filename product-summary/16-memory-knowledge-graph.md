# 16. Memory System & Knowledge Graph

## The Memory System

### What Gets Stored

The `memories` table stores 6 distinct types of memories, each with its own source lifecycle:

| Memory Type | Source | How It's Created |
|-------------|--------|-----------------|
| `note` | Telegram, Pulse | Messages classified as NOTE → embedded → stored |
| `outcome` | Pulse, API | Task completion triggers `write_outcome_memory()` |
| `reflection` | Pulse | After-action report generated nightly |
| `relationship_note` | Email | FYI emails with `has_memory_value=true` |
| `Journal` | Journal | Google Sheets journal entries via `archive_ingest.py` |
| `archive` | Journal | Google Sheets journal entries via `archive_ingest.py` |

### Emotional Metadata

Each memory now stores emotional context as structured fields to enable temporal sentiment queries without polluting the graph:

| Field | Type | Purpose |
|-------|------|---------|
| `sentiment_score` | REAL | -1.0 to +1.0, machine-readable for aggregation |
| `sentiment` | TEXT | Single-word label (e.g., "frustrated", "grateful") |
| `entities_mentioned` | TEXT[] | Named entities found in the text (e.g., ["Atna", "Shirley"]) |

Extracted at ingestion time by Flash Lite during NOTE classification. Enables queries like "how do I feel about Atna?" → aggregate sentiment_score over time → trajectory.

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
- `graph_nodes` — vertices with type and label
- `graph_edges` — directed edges with relationship type and weight

### Node Types (5 types only)

| Type | Created By | Metadata |
|------|-----------|----------|
| `person` | Graph approval flow (pending → approved) | people_id, source |
| `organization` | Graph approval flow | source |
| `project` | Graph approval flow | project_id, org_tag |
| `place` | Backfill extraction | source |
| `animal` | Backfill extraction | source |

**Removed types:** `concept`, `emotional_state`, `resource`, `task`, `practice`, `cluster` — these were either junk drawers (concept, emotional_state) or have dedicated tables (resource, task).

### Edge Types (16 types only)

| Relationship | Source → Target | Valid For |
|-------------|----------------|-----------|
| `DISCUSSED_WITH` | Person → Person | Conversations |
| `MET_WITH` | Person → Person | In-person meetings |
| `INTRODUCED` | Person → Person | Someone introduced someone |
| `FRIEND_OF` | Person → Person | Personal friendships |
| `PARENT_OF` | Person → Person | Family |
| `SPOUSE_OF` | Person → Person | Marriage |
| `SIBLING_OF` | Person → Person | Siblings |
| `FAMILY_OF` | Person → Person | Extended family |
| `PET_OF` | Person → Animal | Pet ownership |
| `MENTORS` | Person → Person | Mentorship |
| `WORKS_AT` | Person → Organization | Employment |
| `WORKS_ON` | Person → Project | Project involvement |
| `CLIENT_OF` | Organization → Organization | Client relationship |
| `VENDOR_TO` | Organization → Organization | Vendor relationship |
| `MEMBER_OF` | Person → Organization | Formal membership |
| `SERVES_AT` | Person → Organization | Ministry / volunteer role |

**Banned types (removed):** `RELATES_TO`, `BELONGS_TO`, `AUTHORED`, `FEELS`, `INVOLVES`, `OWNS` — these were catch-all junk drawers. `OWNS` is still used programmatically by the node approval flow (Danny → OWNS → Project), but is excluded from the extraction prompt.

### Human-in-the-Loop Approval Pipeline

All new edges flow through a staging table before reaching the live graph:

```
Backfill extraction
    → pending_graph_edges (status: pending)
    → Decisions UI or Telegram pe{id} callback
    → process_pending_edge_decision() in graph.py
        → approve: resolve node IDs, insert into graph_edges
        → reject: set status = 'rejected' (kept as diagnostic snapshot)
        → edit: update source/target/relationship before approving
```

**`pending_graph_edges` columns:**

| Column | Type | Purpose |
|--------|------|---------|
| `id` | INTEGER (PK) | For Telegram `pe{id}` shortcodes |
| `source_label` | TEXT | Entity label (resolved to node_id on approve) |
| `target_label` | TEXT | Entity label (resolved to node_id on approve) |
| `relationship` | TEXT | One of 16 valid types |
| `source_text` | TEXT | `{table}:{id}` — which source record generated this edge |
| `source_table` | TEXT | `memories` (or `raw_dumps` in legacy data) |
| `status` | TEXT | `pending` | `approved` | `rejected` |
| `confidence` | REAL | Extraction confidence score |

If a label doesn't exist as a `graph_nodes` entry at approval time, the edge is rejected with a message to create the node first — no more auto-created `concept` nodes.

### Graph Edge Expiry (Planned)

`last_confirmed_at` and `valid_until` columns will be added to `graph_edges`. A monthly pulse check will query edges older than 6 months and ask Danny to verify or retire them. Deferred until the graph has been running clean for 3+ months.

### Graph Extraction Backfill (`backfill_graph.py`)

The backfill pipeline processes memories with the following constraints:

**Source filter (MEMORY_TYPES):** Only `Journal`, `note`, `outcome`, `reflection`, `relationship_note`. Excludes: `Prophecy`, `Psalm`, `Prayer`, `Sermon`, `archive`, `canonical_page`, and **all** `raw_dumps` — raw dumps were found to produce 100% hallucinated edges.

**Prompt ontology:** Strict 5 node types and 16 edge types. No catch-all relationship types (no RELATES_TO, BELONGS_TO, AUTHORED, FEELS). No forced AUTHORED or FEELS edges.

**Entity grounding:** The extraction prompt receives the full list of approved `graph_nodes` (person, organization, project) to match against. New entities outside this list are only created if they are clearly identifiable places or animals.

**Text-anchoring validation:** After LLM extraction, every node label is verified against the source text (case-insensitive substring match). Hallucinated labels are dropped with an audit warning, along with their edges.

### People Table Linkage

The `people` table now has a `graph_node_id` FK → `graph_nodes.id` for person-type nodes. This bridges the two registries — the graph knows the relationship (Marcus → CLIENT_OF → Equisoft), the people table knows the context (role, strategic_weight, last_interaction_date). 89/99 people records were backfilled via label matching.

### Graph Integrity Safeguards

1. **Guard A: Orphaned BELONGS_TO edge cleanup** — When a task's project_id changes, stale edges are deleted before new ones are inserted.
2. **Guard B: Text-anchoring validation** — Node labels must appear verbatim in source text.
3. **HITL: Pending approval** — All edges and high-risk nodes (person, organization, project) require manual approval before reaching the live graph.
4. **Guard D: Dedup** — Unique index on `lower(trim(label))` prevents label-drift re-insertion in `pending_graph_nodes`.
5. **No auto-created concept nodes:** Edge approval no longer creates `concept` nodes for missing labels — missing labels generate a rejection with guidance to create the node first.

### Session Memory (Cross-Pulse Continuity)

To maintain context across time without bloated prompt windows, Rhodey implements Session Memory:
1. After generating a Pulse, the agent summarizes the briefing (assigned tasks, key decisions) in 1-2 sentences.
2. This summary is saved to the `core_config` table under `last_pulse_summary`.
3. At the *start* of the next pulse, this summary is retrieved and injected as `SESSION MEMORY`.
This gives the agent "cross-pulse continuity," allowing it to refer to what it recommended in the last session.

### Visual Exploration

The frontend renders the knowledge graph as an interactive D3.js force-directed visualization with:
- 6 node colors (person, organization, project, place, animal, danny)
- Zoom (0.2x-4x scale)
- Drag with force reheat
- Hover effects (node enlargement, edge highlighting)
- Click to open NodeFlyout detail panel
- 250-tick simulation with auto-stop
