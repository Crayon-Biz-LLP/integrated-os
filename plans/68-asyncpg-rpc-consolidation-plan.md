# Phase 68: Asyncpg + RPC Consolidation — Final Implementation Plan

**Status: ⚠️ IMPLEMENTED THEN PARTIALLY REVERTED (commits `67d2a30` + `0495824`, Jul 2026)**

> **What happened:** Phase 2a/2b/2d (asyncpg pool, `rpc_get_associative_data` /
> `rpc_get_memory_metadata` RPCs, search.py wiring) shipped, but **Phase 2c (hot-path
> conversion) was REVERTED with measured evidence that asyncpg was SLOWER**: through the
> Supavisor pooler asyncpg added 5s+/query (DNS+SSL overhead); direct connection was still
> 1-2s vs PostgREST. PostgREST on the same Supabase infra wins via pre-warmed HTTP pools and
> prepared statements. **Verified: entity_done dropped 19.5s → 1.0s (18.5s saved) by reverting.**
>
> **Current state:** `core/services/async_db.py` (pool) remains and is used by ONE guarded
> fast-path in `core/webhook/classify.py` (PostgREST fallback on any error). The two RPCs in
> `db/70_*.sql` / `db/71_*.sql` exist but are NOT called by the live pipeline (search.py uses
> `search_phrase_nodes` / `match_memories_hybrid`). **Do NOT re-attempt the asyncpg hot-path
> without new evidence** — the revert commit documents why it loses to PostgREST here.

**Status**: Approved after 3 external reviews (9 issues identified and resolved)
**Target**: Save 7-10s per query (31s → ~18-21s average)
**Strategy**: Hybrid — only hot-path reads (~109 calls) migrate. Writes, scripts, and cold paths stay on PostgREST.
**PPR**: Stays in Python (not touched)

---

## The Problem (Current State)

A single user query triggers ~109+ `supabase.table().execute()` calls across the hot path, each going through PostgREST HTTP (TLS + JSON serialization + network). That's ~6-8s of pure HTTP overhead. On top of that, the associative retrieval pipeline makes 7-9 sequential HTTP round trips adding another ~5-7s.

**Total spent on DB access: 11-15s per query.**

## The Solution

Two complementary changes:
1. **asyncpg** — Replace PostgREST HTTP with direct PostgreSQL wire protocol (~400ms → ~15ms per call)
2. **Two RPC functions** — Consolidate 15 sequential DB calls into 2 round trips

---

## Corrections Log (All 9 from 3 Reviews)

| # | Issue | Found by | Fix |
|---|---|---|---|
| 1 | JSONB type mismatch (asyncpg returns strings, not dicts) | Review 1 | `set_type_codec('jsonb', ...)` in pool init |
| 2 | Timestamp type mismatch (datetime vs ISO string) | Review 1 | `SELECT created_at::text` in RPCs |
| 3 | UUID type mismatch (UUID object vs string) | Review 2 | `SELECT id::text` in RPCs |
| 4 | Dangling CTE (subgraph_edges defined but never selected) | Review 2 | Return edges as JSONB in RPC result |
| 5 | Payload duplication (edges repeated 50x) | Review 3 | Single-row composite payload `(nodes jsonb, edges jsonb)` |
| 6 | Correlated subqueries (N scans vs set-based) | Review 2 | Rewrite as CTEs with LEFT JOIN |
| 7 | tsquery crash (special chars in user input) | Review 1 | Keep _build_tsquery() in Python |
| 8 | Event loop binding (asyncpg on wrong loop) | Review 2 | FastAPI lifespan, not Modal class |
| 9 | Pooler URL (must use port 6543) | Review 1 | Already handled in _build_pg_dsn |

---

## Phase 2a: Wire asyncpg into Modal

**Files to modify:**

### `core/services/async_db.py` (modify)

**Changes:**
- Add JSONB codec registration via `init` callback on pool creation
- Explicit error handling for connection failures
- Validate environment variables at init time

**Final code:**
```python
import json

async def _init_conn(conn):
    """Initialize connection with JSONB codec for PostgREST compatibility."""
    await conn.set_type_codec(
        'jsonb',
        encoder=json.dumps,
        decoder=json.loads,
        schema='pg_catalog'
    )

async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not service_key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

    dsn = _build_pg_dsn(supabase_url, service_key)

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        init=_init_conn,  # ← NEW: register JSONB codec
        **_POOL_CONFIG
    )
    return _pool
```

### `api/index.py` (modify)

**Changes:**
- Add FastAPI lifespan event to initialize and close the asyncpg pool

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    from core.services.async_db import init_pool, close_pool
    await init_pool()
    yield
    await close_pool()

app = FastAPI(lifespan=lifespan)
```

### `infra/modal_app.py` (no change needed)

Modal's `@modal.asgi_app()` already respects FastAPI's lifespan events. No decorator changes needed.

**Effort:** 30 min
**Risk:** None (asyncpg is dead code until wired — if it fails, PostgREST still works)
**Time saved:** 0s (enables the rest)

---

## Phase 2b: SQL Migration — Two RPC Functions

### Migration: `db/70_rpc_get_associative_data.sql` (NEW)

```sql
CREATE OR REPLACE FUNCTION rpc_get_associative_data(
    p_tsquery text,       -- Pre-sanitized by Python's _build_tsquery()
    p_limit int DEFAULT 50
)
RETURNS TABLE (
    nodes jsonb,          -- Array of matched phrase nodes with passage/memory links
    edges jsonb           -- Array of graph edges for Python PPR engine
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH matched_nodes AS (
        SELECT pn.id, pn.normalized_text, pn.display_text, pn.node_type,
               ts_rank(pn.fts_vector, to_tsquery('simple', p_tsquery)) AS rank
        FROM retrieval_phrase_nodes pn
        WHERE pn.fts_vector @@ to_tsquery('simple', p_tsquery)
        ORDER BY rank DESC
        LIMIT p_limit
    ),
    linked_passages AS (
        SELECT ppl.node_id,
               ARRAY_AGG(DISTINCT ppl.passage_id) AS passage_ids,
               ARRAY_AGG(DISTINCT mb.memory_id) AS memory_ids
        FROM retrieval_passage_phrase_links ppl
        LEFT JOIN retrieval_memory_bundle_links mb ON mb.passage_id = ppl.passage_id
        WHERE ppl.node_id IN (SELECT mn.id FROM matched_nodes mn)
        GROUP BY ppl.node_id
    ),
    node_payload AS (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id', mn.id::text,
                'normalized_text', mn.normalized_text,
                'display_text', mn.display_text,
                'node_type', mn.node_type,
                'rank', mn.rank,
                'passage_ids', COALESCE(lp.passage_ids, '{}'),
                'memory_ids', COALESCE(lp.memory_ids, '{}')
            )
        ) AS nodes_json
        FROM matched_nodes mn
        LEFT JOIN linked_passages lp ON lp.node_id = mn.id
    ),
    edge_payload AS (
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object('from', e.from_node_id, 'to', e.to_node_id, 'weight', e.weight)
            ),
            '[]'::jsonb
        ) AS edges_json
        FROM (
            SELECT from_node_id, to_node_id, weight
            FROM retrieval_edges
            WHERE from_node_id IN (SELECT id FROM matched_nodes)
               OR to_node_id IN (SELECT id FROM matched_nodes)

            UNION ALL

            SELECT from_node_id, to_node_id, weight
            FROM retrieval_alias_edges
            WHERE from_node_id IN (SELECT id FROM matched_nodes)
        ) e
    )
    SELECT
        COALESCE((SELECT nodes_json FROM node_payload), '[]'::jsonb),
        COALESCE((SELECT edges_json FROM edge_payload), '[]'::jsonb);
END;
$$;
```

### Migration: `db/71_rpc_get_memory_metadata.sql` (NEW)

```sql
CREATE OR REPLACE FUNCTION rpc_get_memory_metadata(
    p_memory_ids int[],
    p_query_embedding vector(768) DEFAULT NULL
)
RETURNS TABLE (
    memory_id       int,
    content         text,
    memory_type     text,
    created_at      text,          -- ::text cast for PostgREST compat
    expires_at      text,
    importance_score float,
    project_id      int,
    semantic_score  float,
    specificity_score float
)
LANGUAGE plpgsql STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH semantic_scores AS (
        SELECT rp.memory_id,
               MAX(1 - (rp.embedding <=> p_query_embedding)) AS score
        FROM retrieval_passages rp
        WHERE rp.memory_id = ANY(p_memory_ids)
          AND p_query_embedding IS NOT NULL
        GROUP BY rp.memory_id
    ),
    specificity_scores AS (
        SELECT mb.memory_id,
               MAX(rns.specificity_score) AS score
        FROM retrieval_memory_bundle_links mb
        JOIN retrieval_passage_phrase_links ppl ON ppl.passage_id = mb.passage_id
        JOIN retrieval_node_stats rns ON rns.node_id = ppl.node_id
        WHERE mb.memory_id = ANY(p_memory_ids)
        GROUP BY mb.memory_id
    )
    SELECT
        m.id,
        m.content,
        m.memory_type,
        m.created_at::text,       -- ISO string for PostgREST compat
        m.expires_at::text,
        COALESCE(m.importance_score, 5) / 10.0,
        m.project_id,
        COALESCE(ss.score, 0.0),
        COALESCE(sps.score, 0.5)
    FROM memories m
    LEFT JOIN semantic_scores ss ON ss.memory_id = m.id
    LEFT JOIN specificity_scores sps ON sps.memory_id = m.id
    WHERE m.id = ANY(p_memory_ids)
      AND m.is_current = TRUE
      AND (m.expires_at IS NULL OR m.expires_at > NOW());
END;
$$;
```

**Effort:** 4-6 hours
**Risk:** Low (independent, testable in SQL Editor first)
**Time saved:** 3-4.5s

---

## Phase 2c: asyncpg Hot-Path Conversion

### Files to modify (ranked by impact):

#### `core/webhook/handler.py` (50 calls → ~30 stay on PostgREST)

Convert:
- `core_config` fetch (audio/doc path → async_fetch)
- `projects` / `organizations` / `people` lookups → async_fetchrow
- `pending_graph_clarifications` check → async_fetchrow
- `pending_nodes` awaiting_details queries → async_fetch

Keep on PostgREST:
- All INSERT/UPDATE/DELETE (writes)
- `processed_updates` dedup check
- `conversations` and `conversation_threads` writes

#### `core/webhook/dispatch.py` (31 calls → ~15 stay)

Convert:
- Graph entity lookups → async_fetchrow
- Calendar event fetches → async_fetch
- Canonical page lookups → async_fetchrow

#### `core/lib/conversation.py` (21 calls → ~10 stay)

Convert:
- Thread reads: `resolve_thread`, `get_history`, `get_thread_summary` → async_fetchrow
- Entity candidate resolution → async_fetch

#### `core/pulse/context.py` (13 calls → ~5 stay)

Convert:
- Core config fetches → async_fetch
- Graph node entity lookups → async_fetchrow

### Conversion pattern:

```python
# Before (PostgREST):
res = supabase.table('projects').select('id, name').eq('id', pid).execute()
row = res.data[0] if res.data else None

# After (asyncpg):
from core.services.async_db import async_fetchrow
row = await async_fetchrow("SELECT id, name FROM projects WHERE id = $1", pid)
```

### Type safety checklist — EVERY SELECT must verify:

| Column type | In SQL | Python receives |
|---|---|---|
| timestamptz | `SELECT created_at::text` | `"2026-07-25T12:00:00+00:00"` (string) |
| uuid | `SELECT id::text` | `"550e8400-e29b-..."` (string) |
| jsonb | auto-decoded by codec | Python dict/list |
| int, float, text | no cast needed | native types |
| numeric | `SELECT score::float8` | Python float |

**Effort:** 4-6 hours
**Risk:** Low-Medium (read-only, no writes via asyncpg)
**Time saved:** 4-6s

---

## Phase 2d: Wire RPCs into search.py

### Modify `core/retrieval/search.py`

**Before** (current flow, ~18 DB calls):
```
1. LLM entity extraction        (2 DB calls)
2. search_phrase_nodes RPC      (1 DB call)
3. fetch retrieval_edges        (2 DB calls)
4. fetch alias_edges            (1 DB call)
5. run PPR                      (Python, 50ms)
6. aggregate to passages/mems   (3-4 DB calls)
7. filter expired memories      (1 DB call)
8. fetch metadata + embeddings  (3 DB calls)
9. fetch specificity scores     (2-3 DB calls)
10. rank memories               (Python)
11. assemble bundles            (3-4 DB calls)
```

**After** (~3 DB calls):
```
1. LLM entity extraction        (2 DB calls, stays)
2. rpc_get_associative_data     (1 RPC call) ← NEW
3. run PPR                      (Python, 50ms, stays)
4. rpc_get_memory_metadata      (1 RPC call) ← NEW
5. rank memories                (Python, stays)
6. assemble bundles             (3-4 DB calls, stays)
```

**Key code changes in `associative_retrieve()`:**

```python
from core.services.async_db import async_fetchrow, async_fetch

async def associative_retrieve(...):
    start = time.time()

    # Phase 1: Entity extraction (same as before)
    llm_phrases = await _get_cached_entities()
    query_emb = await _get_cached_embedding()
    lex_phrases = _parse_query(query)

    # Phase 2: Single RPC call instead of 5
    tsquery = _build_tsquery(lex_phrases + llm_phrases)
    row = await async_fetchrow(
        "SELECT nodes, edges FROM rpc_get_associative_data($1, $2)",
        tsquery, 30
    )

    # ⚠️ JSONB codec decodes to native Python list, not string
    # Defensive: handle both decoded list and string fallback
    nodes_raw = row["nodes"] if row else []
    edges_raw = row["edges"] if row else []
    nodes = nodes_raw if isinstance(nodes_raw, list) else json.loads(nodes_raw)
    edges = edges_raw if isinstance(edges_raw, list) else json.loads(edges_raw)

    if not nodes or not edges:
        return ExplainableBundle(query=query, items=[], latency_ms=...)

    # Phase 3: Recognition filter (Python — same as before)
    filtered_nodes = _recognition_filter(nodes, query_phrases)

    # Phase 4: PPR (Python — same as before)
    seed_nodes = {n["id"]: n.get("rank", 0.5) for n in filtered_nodes}
    # Build adjacency from edges returned by RPC
    adjacency = build_adjacency_from_edges([
        (e["from"], e["to"], e["weight"]) for e in edges
    ])
    ppr_raw = personalized_pagerank(adjacency, seed_nodes)
    ppr_norm = normalize_scores(ppr_raw)

    # Aggregate: memory scores come from RPC's linked passages
    memory_scores = {}
    for n in filtered_nodes:
        for mid in n.get("memory_ids", []):
            memory_scores[mid] = max(memory_scores.get(mid, 0), ppr_norm.get(n["id"], 0))

    # Phase 5: Fetch memory metadata via TABLE-returning RPC
    # rpc_get_memory_metadata returns a TABLE (one row per memory), not a JSON blob.
    # Use async_fetch to get all rows, then build boost dicts from the result set.
    memory_ids = list(memory_scores.keys())
    if not memory_ids:
        return ExplainableBundle(query=query, items=[], latency_ms=...)

    meta_rows = await async_fetch(
        "SELECT * FROM rpc_get_memory_metadata($1, $2::vector(768))",
        memory_ids, query_emb
    )

    # Build boost dicts from tabular results
    recency_boost = {}
    importance_boost = {}
    project_boost = {}
    semantic_scores = {}
    specificity_boost = {}

    for r in meta_rows:
        mid = r["memory_id"]
        # recency: compute from created_at::text (ISO string)
        if r.get("created_at"):
            created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            days_old = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
            recency_boost[mid] = max(0.0, 1.0 - days_old / 90.0)
        else:
            recency_boost[mid] = 0.0
        importance_boost[mid] = r.get("importance_score", 0.5)
        project_boost[mid] = 1.0 if active_project_id and r.get("project_id") == active_project_id else 0.0
        semantic_scores[mid] = r.get("semantic_score", 0.0)
        specificity_boost[mid] = r.get("specificity_score", 0.5)

    # Phase 6: Ranking (Python — same)
    ranked = rank_memories(
        memory_scores=memory_scores,
        ppr_scores=memory_scores,
        semantic_scores=semantic_scores,
        specificity_boost=specificity_boost,
        recency_boost=recency_boost,
        importance_boost=importance_boost,
        project_boost=project_boost,
        person_boost=person_boost or {},
    )

    # Phase 7: Bundle assembly (stays in Python)
    items = await asyncio.to_thread(_assemble_bundles, ranked[:top_k], ppr_norm, list(seed_nodes.keys()))

    return ExplainableBundle(
        query=query,
        items=items,
        total_candidates=len(ranked),
        latency_ms=int((time.time() - start) * 1000),
    )
```

**Note:** Because `rpc_get_memory_metadata` returns a TABLE (multiple rows), we use `async_fetch` instead of `async_fetchrow`. The JSONB codec automatically decodes all columns to native Python types.

**Effort:** 2-3 hours
**Risk:** Medium (must match RPC output format exactly)
**Time saved:** enables the RPC savings

---

## Testing Plan

### Phase 2a test (asyncpg wiring):
```python
# Run on Modal serve
curl https://*.modal.run/api/health
# Verify in logs: "asyncpg pool initialized successfully"
```

### Phase 2b test (RPC functions):
```sql
-- Run in Supabase SQL Editor
SELECT * FROM rpc_get_associative_data('qhord | amita | funds', 10);
SELECT * FROM rpc_get_memory_metadata(ARRAY[2617, 2277, 2263], NULL::vector(768));
```

### Phase 2c test (hot-path conversion):
```bash
# Run existing integration tests
LIVE_DB=true PYTHONPATH=. pytest tests/sim/test_context_registry.py -v
LIVE_DB=true PYTHONPATH=. pytest tests/sim/test_simulated_flows.py -v
```

### Phase 2d test (end-to-end):
```bash
# Send real queries to Telegram and measure latency
# Compare against the latency table below
```

---

## Rollback Strategy

Each phase is independently rollbackable:

| Phase | Rollback |
|---|---|
| **2a** | Comment out lifespan in api/index.py — PostgREST still works |
| **2b** | DROP FUNCTION rpc_get_associative_data — Python falls back to old code |
| **2c** | Comment out async_fetch, uncomment supabase.table — one line per call |
| **2d** | Keep old associative_retrieve() as fallback path behind config flag |

---

## Expected Latency Improvement

| Query | Current (Modal) | After asyncpg + RPC | Δ |
|---|---|---|---|
| Schedule (meetings) | 28s | **~18s** | ⚡ -36% |
| People (Sunjula) | 27s | **~17s** | ⚡ -37% |
| Status (Qhord) | 32s | **~18s** | ⚡ -44% |
| General (Ashraya) | 33s | **~19s** | ⚡ -42% |
| Status (Equisoft) | 36s | **~20s** | ⚡ -44% |
| **Average** | **~31s** | **~18s** | ⚡ **-42%** |

The ~10s LLM floor limits further improvement. Beyond this, only model swaps or prompt compression would help.

---

## Effort Summary

| Phase | What | Effort | Risk | Time saved |
|---|---|---|---|---|
| 2a | Wire asyncpg into Modal | 30 min | None | 0s (enables) |
| 2b | Two SQL RPC functions | 4-6 hours | Low | 3-4.5s |
| 2c | asyncpg hot-path conversion | 4-6 hours | Low-Med | 4-6s |
| 2d | Wire RPCs into search.py | 2-3 hours | Medium | enables 2b |
| Test | Ruff + integration tests | 1-2 hours | — | — |
| **Total** | | **~12-18 hours** | **Low-Med** | **~7-10s** |

---

## Pre-Execution Checklist (Do Before Any Code Changes)

1. **Run SQL migrations first**: Execute `db/70_rpc_get_associative_data.sql` and `db/71_rpc_get_memory_metadata.sql` in Supabase SQL Editor before deploying any code changes to Modal.

2. **Verify pool connection limits**: Confirm Supabase's PgBouncer connection limit can handle Modal's concurrency. With `max_size=5` per pool and `max_inputs=10` concurrent requests per Modal container, worst case is 50 connections across 10 concurrent handlers. Supabase Pro plan allows 60 direct connections — fine. Adjust `_POOL_CONFIG.max_size` if needed.

3. **All ::text casts verified**: Every SELECT query converted from PostgREST to async_fetch must include explicit `::text` casts on UUID and TIMESTAMPTZ columns to match existing downstream model expectations.

---

## What Does NOT Change

- **PPR** → stays in Python ✅
- **LLM calls** → stays as-is ✅
- **Telegram sendMessage** → stays as-is ✅
- **Writes** (INSERT, UPDATE, DELETE) → stay on PostgREST ✅
- **Scripts** (backfill, repair, analyze) → stay on PostgREST ✅
- **API endpoints** (admin, configuration) → stay on PostgREST ✅
- **Sentinel, engine, pulses** → stay on PostgREST ✅
- **Vercel deployment** → already decommissioned, Modal is the sole backend ✅
- **All product-summary docs** → no changes needed ✅
