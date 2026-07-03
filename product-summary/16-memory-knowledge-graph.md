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

### 7-Signal Associative Ranking

The primary retrieval path uses `associative_retrieve()` (`core/retrieval/search.py`) — a 7-signal ranking pipeline that replaced the legacy pgvector-only approach:

1. **Semantic Similarity:** Query embedding (768-dim Gemini) is cosine-scored against all matched passages.
2. **Personalized PageRank (PPR):** Graph traversal from matched phrase nodes surfaces indirect connections (~50ms on bounded subgraph).
3. **Recency Decay:** Exponential curve `EXP(-days / 15.0)` — 30-day half-life.
4. **Importance Score:** Memory's `importance_score` (1-10 scale) directly factored into rank.
5. **Project Boost:** Cross-references memories linked to active projects via graph edges.
6. **Specificity Boost:** Node degree weighting — more specific entities rank higher.
7. **Person Boost:** Active person anchor biases results toward memories mentioning that person (+5% rank weight).

All signals are blended with configurable weights in `core/retrieval/ranking.py`. The legacy `match_memories_hybrid` RPC remains as a fallback path.

### Associative Retrieval Pipeline

Rhodey's associative retrieval runs in three phases:

**Phase 1 — Query Analysis (parallel via `asyncio.gather()`):**
- **LLM Entity Extraction:** Query sent to Gemini Flash Lite (`CLASSIFICATION_MODEL`) to extract entities, cached for 1h via Redis SHA-256 key.
- **Lexical Phrase Splitting:** Query split into word n-grams (1-3 words) for trigram matching.
- **Embedding:** Query embedded via `gemini-embedding-2-preview`, cached for 24h via Redis.

**Phase 2 — Graph Traversal:**
- Matched phrase nodes drive `personalized_pagerank()` across the bounded subgraph.
- LLM-only entities trigger a secondary DB fetch for phrase nodes not caught by lexical match.
- Alias edges (`retrieval_alias_edges`) bridge synonymous labels identified via heuristics.

**Phase 3 — Aggregation & Ranking:**
- Passages aggregated to memories via nested PostgREST joins (collapsed N+1 queries).
- 7-signal rank blended → deduplicated → top-k returned as `ExplainableBundle`.

The LLM receives a unified context block where associative signals and graph structure are co-presented.

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

All queries run concurrently via `asyncio.gather()`. Each query path calls `search_memories_compat()`, which routes to `associative_retrieve()` when enabled (all feature flags ON as of June 2026). Results are deduplicated by ID, sorted by hybrid score, and the top-k returned.

## The Knowledge Graph

### Structure

The graph is stored in two tables:
- `graph_nodes` — vertices with type and label
- `graph_edges` — directed edges with relationship type and weight

### Node Types (5 core + concept)

| Type | Created By | Metadata |
|------|-----------|----------|
| `person` | Graph approval flow (pending → approved) | people_id, source |
| `organization` | Graph approval flow | source |
| `project` | Graph approval flow | project_id, organization_name |
| `place` | Backfill extraction | source |
| `animal` | Backfill extraction | source |
| `concept` | Concept sweep batch + HITL approval | Deduped via 85%+ similarity check |

**Concept Fluidity (Synaptic Plasticity):** Abstract `concept` nodes were re-introduced after the June ontology overhaul. They are extracted via `concept_sweep_batch.py` from historical memories and flow through the same HITL pipeline as person/org/project nodes. A proactive `find_similar_node()` check at 85%+ similarity offers a 1-click `[Merge into this]` button, preventing label drift.

**Removed types that stay removed:** `emotional_state`, `resource`, `task`, `practice`, `cluster` — these were either junk drawers or have dedicated tables.

### Edge Types (16 core + 3 concept)

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

### Concept Edge Types (3 types)

In addition to the 16 core types, `concept` nodes have their own relationship vocabulary:

| Relationship | Source → Target | Purpose |
|-------------|----------------|---------|
| `EVOKES` | Concept → Concept | One concept reminds the system of another |
| `RELATES_TO` | Concept → Concept | General thematic connection |
| `ASSOCIATED_WITH` | Concept → Entity | Abstract concept linked to a concrete entity |

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
3. **HITL: Pending approval** — All edges and high-risk nodes (person, organization, project, concept) require manual approval before reaching the live graph.
4. **Guard D: Dedup** — Unique index on `lower(trim(label))` prevents label-drift re-insertion in `pending_graph_nodes`.
5. **Concept Fluidity:** Abstract `concept` nodes are supported but never auto-created. They flow through `pending_graph_nodes` with 85%+ similarity dedup detection and 1-click merge confirmation. Concept edge types (`EVOKES`, `RELATES_TO`, `ASSOCIATED_WITH`) are part of the `VALID_EDGE_MATRIX`.

### Session Memory (Cross-Pulse Continuity)

To maintain context across time without bloated prompt windows, Rhodey implements Session Memory:
1. After generating a Pulse, the agent summarizes the briefing (assigned tasks, key decisions) in 1-2 sentences.
2. This summary is saved to the `core_config` table under `last_pulse_summary`.
3. At the *start* of the next pulse, this summary is retrieved and injected as `SESSION MEMORY`.
This gives the agent "cross-pulse continuity," allowing it to refer to what it recommended in the last session.

## Associative Retrieval Architecture (June 2026)

### Database Tables

The retrieval layer uses 7 dedicated tables, separate from the main `memories` and `graph_*` tables:

| Table | Purpose | Row Count |
|---|---|---|
| `retrieval_passages` | Chunked memory passages (512-char windows) | 855 |
| `retrieval_phrase_nodes` | Extracted phrase entities with embedding | ~1500 |
| `retrieval_node_stats` | Per-node degree/frequency statistics | ~1500 |
| `retrieval_passage_phrase_links` | Many-to-many: passages ↔ phrase nodes | 2344 (704 enriched passages linked) |
| `retrieval_memory_bundle_links` | Many-to-many: retrieval data ↔ memory IDs | ~850 |
| `retrieval_alias_edges` | Heuristic synonymous label bridges | 3760 |
| `retrieval_index_runs` | Index operation checkpoint tracking | ~480 |

### Chunk Enrichment (Entity Prefix)

When `RETRIEVAL_CHUNK_ENRICHMENT=true` (set in production), passages are re-embedded after entity extraction with an enrichment prefix prepended to the text: `[retrieval, entity1, entity2, entity3] original text...`. This aligns the embedding space of passages with the query-side enrichment in `associative_retrieve()`, improving semantic match for entity-bearing queries.

Of 855 indexed passages:
- **704** have entity labels extracted (enriched with `[retrieval, entity1, …]` prefix)
- **151** have a plain `[retrieval]` prefix (LLM extracted no entities — only vector-search discoverable)

### Forward Indexing Pipeline

Indexing is triggered via the `pending_retrieval_index_jobs` table (written by `schedule_index_memory()`), processed by the Sentinel piggyback via `process_pending_index_jobs()`. This decouples LLM-heavy work from the webhook response path.

```
pending_retrieval_index_jobs.insert
    → process_pending_index_jobs()              # Sentinel piggyback
        → index_memory(memory_id)
            → chunk_into_passages(text)         # 512-char sliding windows
            → Phase 1: embed all passages       # parallel asyncio.gather
            → Phase 2: extract entities         # Gemini Flash Lite, concurrency 3
            → build_triple_graph()              # batch-resolve nodes, batch-upsert
                → node resolution (one query)
                → new node creation (parallel embeddings)
                → edge batch upsert (deduped)
                → link batch upsert (deduped)
                → alias linking (per-node)
            → Phase 3: re-embed with entities   # if chunk_enrichment enabled
            → update_node_stats()               # after all jobs complete
        → mark job completed / dead_letter
```

### `build_triple_graph()` Batch Protocol

The function (`core/retrieval/graph.py`) was refactored from per-triple sequential to batch operations:

1. **Node resolution:** One query to `retrieval_phrase_nodes` for all normalized texts, then only create missing nodes in parallel.
2. **Edge batch upsert:** Collects all edges from all triples, deduplicates on `(from_node_id, to_node_id, edge_type, index_version)` keeping max weight, then single batch upsert.
3. **Link batch upsert:** Collects all links from all triples, deduplicates on `(passage_id, node_id, role)` keeping max weight, then single batch upsert.

**History:** The initial per-triple implementation caused 342 link and 21 edge upsert failures during backfill — each triple's upsert was sent separately, and duplicate constrained tuples within a batch triggered Postgres `ON CONFLICT DO UPDATE command cannot affect row a second time`. The batch + dedup fix and a one-time repair script (`scripts/repair_missing_links.py`) restored full coverage: all 704 enriched passages now have ≥1 phrase link.

Indexing runs at concurrency 3 (module-level `asyncio.Semaphore(3)`), with jittered backoff on Gemini 429s via multi-key rotation.

### Performance Characteristics

| Metric | Cold Path | Warm Path |
|--------|-----------|-----------|
| **Total latency** | 3.5–5.0s | 1.8–3.5s |
| LLM entity extraction | ~2.5s | ~10ms (Redis cache) |
| Embedding fetch | ~1.2s | ~10ms (Redis cache) |
| Post-Gemini tail | ~900ms | ~900ms |
| PPR traversal | ~50ms | ~50ms |
| Lexical phrase search | ~5ms | ~5ms (GIN trigram) |

**Gemini Free Tier limits:** 1000 embed_content requests/day/project/model. Mitigated by 3 API keys (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`) with transparent multi-key failover on 429 errors. Effective daily limit: ~3000 before exponential backoff.

### Redis Caching

Two cache tiers, both in Upstash Redis, fail-open on Redis error:

| Cache | Key | TTL | Hit Effect |
|-------|-----|-----|------------|
| LLM entity extraction | `retrieval:entities:{sha256(query)}` | 1h | Skips Gemini Flash Lite call (~2.5s saved) |
| Query embedding | `retrieval:embedding:{sha256(query)}` | 24h | Skips embed_content call (~1.2s saved) |

### Feature Flags (Env Vars)

Five per-site flags control which read paths use associative retrieval. All are set to `true` in production (Vercel env vars):

| Flag | Path Activated | Purpose |
|------|----------------|---------|
| `RETRIEVAL_ASSOCIATIVE_ENTITY_SUMMARY` | `brain_synth.py` | Entity summaries use associative |
| `RETRIEVAL_ASSOCIATIVE_RECENT_MEMORIES` | `memory.py` (briefing) | Recent memories use associative |
| `RETRIEVAL_ASSOCIATIVE_HINDSIGHT` | `memory.py` (hindsight) | Hindsight retrieval uses associative |
| `RETRIEVAL_ASSOCIATIVE_HYDRATE` | `context.py` | Context hydration uses associative |
| `RETRIEVAL_INDEXING_ENABLED` | `pipeline.py` | Forward indexing is live |
| `RETRIEVAL_CHUNK_ENRICHMENT` | `pipeline.py` | Passage chunk entity prefix enrichment |

### Data Integrity

- **Checkpoint/resume:** `retrieval_index_runs` tracks memory runs by `memory_id` + `source_type`. Backfill resumes from last checkpoint.
- **GIN trigram index:** `idx_phrase_nodes_text` on `normalized_text` using `gin_trgm_ops` — phrase lookups at ~5ms.
- **Alias edge backfill:** 3760 heuristic edges bridge synonymous labels (e.g., "Paulsons" ↔ "Paulsons Ledgers").
- **No shadow mode:** Legacy pgvector path (`match_memories_hybrid`) remains as fallback but is no longer the primary.

## Memory Expiry Enforcement

Time-bound memories (e.g. "remind me next week", "expires in 2h") are stored with `expires_at` set at ingestion. The **associative retrieval** path filters expired memories in `associative_retrieve()` (post-PPR query against `memories.expires_at`). The legacy `match_memories_hybrid` RPC already had expiry filtering — this was a gap in the newer associative path that is now closed.

## Memory Versioning

Memories have `is_current`, `version`, `supersedes_id` columns (typed `int8`) but are versioned by **application code** (`version_memory_for_update()` in `core/services/db.py`), not by a database trigger. This is the architecture decision — memories are deemed higher-churn and lower-risk than tasks/canonical-pages, so application-level versioning is sufficient.

Two enrichment paths call `version_memory_for_update()` before mutation:
- `dispatch.py:_enrich_memory_entities()` — sets `organization_id`/`project_id` after entity extraction
- `completion_handler.py` — entity enrichment in degraded completion path

**Caveat**: Application-level versioning is easier to bypass than a DB trigger. Any future memory update path that forgets to call `version_memory_for_update()` can reintroduce silent overwrite bugs. A `BEFORE UPDATE` trigger on `memories` is the defence-in-depth layer once confidence in application code patterns is established.

## Deletion / Index Cleanup

The undo delete path in `commands.py` calls `cleanup_memory_retrieval_index()` (in `core/retrieval/cleanup.py`) before deleting a memory. This cascading cleanup removes all rows from `retrieval_passages`, `retrieval_memory_bundle_links`, and `retrieval_index_runs` for the given `memory_id`.

A standalone `sweep_orphan_retrieval_entries()` function runs daily via the Sentinel piggyback and catches any orphans from code paths that bypassed cleanup. **Caveat**: This is cleanup-by-routine, not cleanup-by-constraint. Orphans survive ~20h before the sweep catches them. A foreign key with `ON DELETE CASCADE` or a trigger-based cascade is the stricter alternative.

### Visual Exploration
The brain graph page (`/dashboard/memories/graph`) renders the graph as a split-pane interactive view:
- **Left pane**: Episode Stream — clustered memories grouped by shared entity, source thread, or time window. Cards show title, summary, entity badges (color-coded), and memory count. Click to expand raw memories beneath. Collapsible via toolbar toggle.
- **Right pane**: NeuralDisc — PixiJS v8 WebGL force-directed graph. Danny-centered 2-hop ego graph. Hover highlights connections, click node loads neighborhood, background click returns to Danny. Zoom via mouse wheel (toward cursor), pan via background drag. Zoom controls overlay (+/-/Fit). 7 node colors by type. Breathing glow on center node.
- **Backend**: 4 API endpoints power the view: ego graph, neighborhood, resolve-memory, and episode stream.
- **Performance**: All callback props stored in refs to prevent infinite scene rebuild loops. Dep array reduced from 10 to 5 deps. D3.js layout computed once per data change, PIXI scene rebuilds only on layout/hover changes.
