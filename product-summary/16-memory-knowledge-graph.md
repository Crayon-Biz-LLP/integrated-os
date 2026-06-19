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

## Associative Retrieval Architecture (June 2026)

### Database Tables

The retrieval layer uses 7 dedicated tables, separate from the main `memories` and `graph_*` tables:

| Table | Purpose | Row Count |
|-------|---------|-----------|
| `retrieval_passages` | Chunked memory passages (512-char windows) | 633 |
| `retrieval_phrase_nodes` | Extracted phrase entities with embedding | 1305 |
| `retrieval_node_stats` | Per-node degree/frequency statistics | 1292 |
| `retrieval_passage_phrase_links` | Many-to-many: passages ↔ phrase nodes | 1928 |
| `retrieval_memory_bundle_links` | Many-to-many: retrieval data ↔ memory IDs | 646 |
| `retrieval_alias_edges` | Heuristic synonymous label bridges | 3760 |
| `retrieval_index_runs` | Index operation checkpoint tracking | 470 |

### Forward Indexing Pipeline

Every new memory write triggers `schedule_index_memory()` (`core/retrieval/pipeline.py`):

```
memories.insert
    → schedule_index_memory(memory_id)
        → fetch_memory(memory_id)
        → chunk_into_passages(text)           # 512-char sliding windows
        → upsert_passages(passages)            # save to retrieval_passages
        → extract_entities(passages)           # Gemini Flash Lite extraction
        → upsert_phrase_nodes(entities)        # embed + save to retrieval_phrase_nodes
        → link_passage_phrases(passages, nodes) # build passage_phrase_links
        → link_bundle(memory_id, passage_ids)   # build memory_bundle_links
        → update_node_stats()                   # refresh frequency stats
        → log_index_run()                       # checkpoint
```

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

Four per-site flags control which read paths use associative retrieval. All are set to `true` in production:

| Flag | Path Activated | Purpose |
|------|----------------|---------|
| `RETRIEVAL_ASSOCIATIVE_ENTITY_SUMMARY` | `brain_synth.py` | Entity summaries use associative |
| `RETRIEVAL_ASSOCIATIVE_RECENT_MEMORIES` | `memory.py` (briefing) | Recent memories use associative |
| `RETRIEVAL_ASSOCIATIVE_HINDSIGHT` | `memory.py` (hindsight) | Hindsight retrieval uses associative |
| `RETRIEVAL_ASSOCIATIVE_HYDRATE` | `context.py` | Context hydration uses associative |
| `RETRIEVAL_INDEXING_ENABLED` | `pipeline.py` | Forward indexing is live |

### Data Integrity

- **Checkpoint/resume:** `retrieval_index_runs` tracks memory runs by `memory_id` + `source_type`. Backfill resumes from last checkpoint.
- **GIN trigram index:** `idx_phrase_nodes_text` on `normalized_text` using `gin_trgm_ops` — phrase lookups at ~5ms.
- **Alias edge backfill:** 3760 heuristic edges bridge synonymous labels (e.g., "Paulsons" ↔ "Paulsons Ledgers").
- **No shadow mode:** Legacy pgvector path (`match_memories_hybrid`) remains as fallback but is no longer the primary.

### Visual Exploration
- 6 node colors (person, organization, project, place, animal, danny)
- Zoom (0.2x-4x scale)
- Drag with force reheat
- Hover effects (node enlargement, edge highlighting)
- Click to open NodeFlyout detail panel
- 250-tick simulation with auto-stop
