# 28. Entity Grounding Guards & Clarification Loop

## Overview

Four guard layers that prevent bad data from entering the knowledge graph, plus a clarification loop that enables the OS to ask Danny about low-confidence extractions via Telegram. Together they form the **"verify, hold, and clarify"** layer over the older **"trust and inject"** extraction pipeline.

---

## Guard 1: Prompt Grounding (PROJECT DEFINITION)

**File**: `entity_extractor.py`, `backfill_graph.py` (extraction prompts), `graph.py` (`_infer_additional_edges()` prompt)

Both extraction prompts now include a `PROJECT DEFINITION` section with explicit positive and negative examples:

```
PROJECT DEFINITION:
- What is NOT a project: GitHub repos, open-source libraries (e.g. Supabase, React),
  theoretical concepts, events/conferences, generic work terms (e.g. 'code review', 'frontend').
- What IS a project: Specific professional work streams, client engagements, side projects
  with structure (e.g. Qhord, SOLVSTRAT, Ashraya, Integrated OS).
```

Negative examples are weighted heavier — the LLM already knows what a project is, it doesn't know what Rhodey considers *not* a project.

---

## Guard 2: Entity Grounding for Projects (`is_real_project()`)

**File**: `core/skills/backfill_graph.py:527`

Applied in both code paths:

1. **Batch path** (`upsert_nodes`, `backfill_graph.py:623`): Before inserting any new `project` node into `pending_graph_nodes`, checks if the label matches a project name in the `projects` table via `ILIKE`. If no match → **hard-rejected** with audit log warning. Node never enters pending.

2. **Individual path** (`get_or_create_node`, `backfill_graph.py:550`): Same check. Ungrounded projects are skipped entirely.

3. **Real-time path** (`entity_extractor.py:82`): Same check during webhook ingestion. If no project match → skip.

---

## Guard 3: Structural Anchor for People & Orgs (`has_structural_anchor()`)

**File**: `core/skills/backfill_graph.py:540`

Uses a `GROUNDED_TYPES` dictionary mapping node types to their structured tables:

```python
GROUNDED_TYPES = {
    'project': ('projects', 'name'),
    'person': ('people', 'name'),
    'organization': ('organizations', 'name'),
}
```

- **Project**: Must exist in `projects` table → Guard 2 hard-rejects unmatched ones.
- **Person**: If name matches `people` table → `status='pending'`. If no match → `status='flagged'` (flagged for clarification loop).
- **Organization**: If name matches `organizations` table → `status='pending'`. If no match → `status='flagged'`.

Applied in both `get_or_create_node()` and `upsert_nodes()` in `backfill_graph.py`.

---

## Guard 4: URL Quarantine

**File**: `backfill_graph.py:fetch_memories()`, `core/actions/planner.py`, `entity_extractor.py`

Any text containing `http://` or `https://` is:

1. **Not stored as a memory** — `fetch_memories()` filters out URL-containing records
2. **Not extracted for entities** — `entity_extractor.py` returns early if text contains a URL
3. **Saved only as a resource** — `handler.py` URL quarantine routes URL NOTES to `resources` table instead of `memories`

This prevents bookmark articles, shared links, and URL-containing notes from hallucinating fake entities into the graph.

---

## Step 1.5: Entity Extractor Routing Fix (P1)

**File**: `core/pulse/entity_extractor.py`

Previously, the webhook ingestion path (`extract_and_link_entities()`) wrote:

- Organization nodes directly to `graph_nodes`
- All LLM-extracted edges directly to `graph_edges`

**Both had zero guards or HITL.** This was the highest-frequency path (every Telegram message) and the biggest gap.

**Fix**: Route organizations through `pending_graph_nodes` and all edges through `pending_graph_edges`, matching the behavior of `backfill_graph.py`.

| Entity Type | Before | After |
|-------------|--------|-------|
| Organization | Direct `graph_nodes` insert | `pending_graph_nodes` (HITL) |
| LLM-extracted edge | Direct `graph_edges` insert | `pending_graph_edges` (HITL) |
| Person (grounded) | Direct `graph_nodes` insert | `pending_graph_nodes` (HITL) |
| Person (ungrounded) | Direct `graph_nodes` insert | `pending_graph_nodes` (flagged) |
| Project (grounded) | Direct `graph_nodes` insert | Direct insert (already existed) |
| Project (ungrounded) | Direct `graph_nodes` insert | Hard-rejected by Guard 2 |
| Concept/Place/Animal | Direct `graph_nodes` insert | Direct insert (harmless metadata) |

---

## Organizations Table (Step 2)

```sql
CREATE TABLE organizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  graph_node_id uuid REFERENCES graph_nodes(id)
);
```

- Seeded from existing `graph_nodes` where `type = 'organization'` (excluding merged duplicates)
- Enables Guard 3 structural anchor for organizations
- `GROUNDED_TYPES` updated in `backfill_graph.py` to include `organization → (organizations, name)`
- Grants opened for service_role access

---

## People ↔ Graph Nodes Sync (Step 3)

- One-time SQL: `UPDATE people SET graph_node_id = g.id FROM graph_nodes g WHERE ... lower match`
- 7 direct matches applied (e.g., CPA, Sunju, Reginald Paulson, Graena Lawrance, Gan, Devil, Judas Iscariot)
- 15 remaining unmatched (names that don't exactly match any graph_nodes label) — will be grounded organically as new extractions surface them

---

## Task Node Cleanup (Step 4)

**File**: `scripts/task_node_cleanup.py`

Deletes `graph_nodes` entries for transitional task nodes (`metadata->>source = 'transitional'`) when their source task is `done` or `cancelled`. Safe by design — label-based matching, never deletes a node whose label doesn't match a done/cancelled task. Also checks for remaining edges before deletion to avoid deleting connected nodes.

---

## Clarification Loop — Retired (plans/73)

> **Superseded 2026-08-14 by `plans/73-clarifier-rework-queue-native-graph-hitel.md`.**
> The graph clarification question flow is retired. The app is the primary
> surface (vision `00-vision-and-mindset.md`); asking the user to QA every
> low-confidence extraction via Telegram was inverted labor — the user doing
> Rhodey's work — and the dispatch was structurally late (30-min heartbeat,
> batched ≤5).

### What replaced it

| Old mechanism | Replacement |
|---|---|
| `evaluate_node`/`evaluate_edge` question generation | Retired no-op hooks in `core/clarifier.py` (signatures kept for `backfill_graph`) |
| `awaiting_clarification` status flip (hid items from the queue) | Removed — pending nodes/edges stay in Quick Confirmation as ordinary HITL cards (the architecture's documented edge flow) |
| `clarification_feedback` + shortcode answers + `POST /api/clarification` | Removed — no separate answer surface |
| Sentinel 🧠 batch dispatch + weekly "unanswered clarifications" line | Removed |
| Edge-contradiction question | `graph.py::enrich_pending_edges_with_conflicts()` — renders as a "⚠️ conflicts with existing …" hint on the queue edge card |
| Per-edge "is this correct?" interrogation | Silent gate: single-source LLM edges start at `confidence 0.55`; corroboration (re-mention) raises it; stale low-confidence edges expire via `decision_pulse` (state machine `pending → expired`); the pulse briefing appends one daily check-in line listing newly-tracked unconfirmed items |
| Learning loop | Queue approvals + Entities corrections (rename/type/delete) emit observations (`emit_observation`) — the clarifier answer path never did |

### Migration

`db/98_retire_clarifier_question_flow.sql` reverts legacy `awaiting_clarification` rows to
`pending` (so they reappear in the queue), resolves open `clarification_feedback` rows, and
removes the status from the `pending_nodes` CHECK constraint.

---

## Validation Window (Step 6)

**File**: `scripts/validate_deployment.py`

48-hour operational checkpoint after Step 1.5 deployment:

1. **Snapshot pre-deployment** `pending_graph_nodes` and `pending_graph_edges` counts
2. **After 48h, check for direct-insert org leaks:**
   ```sql
   SELECT label, type, created_at FROM graph_nodes
   WHERE type = 'organization'
     AND created_at > '[step_1.5_deploy_timestamp]'
     AND id NOT IN (
       SELECT graph_node_id FROM pending_graph_nodes WHERE graph_node_id IS NOT NULL
     );
   ```
   Must return zero rows. If any → routing gap in Step 1.5.
3. **Spot-check** 3-5 new pending records via Decisions UI
4. **Manual greenlight** before declaring deployment clean

The `created_at` column was added to `graph_nodes` via `ALTER TABLE` to enable this timestamp-based filtering. Legacy data receives the current timestamp; new inserts are correctly timestamped.

---

## Deferred (Tracked)

### Practices Module (`core/pulse/practices.py`)
Auto-detected practices (via LLM-based pattern matching) create nodes + ASSOCIATED_WITH edges bypassing all guards. Deferred post-Phase 2 — will add confidence gate + pending routing alongside Phase 4 correction learning build-out (30+ corrections threshold).

### Phase 4 (Learning)
Superseded by the observation wiring on queue decisions and Entities corrections
(`emit_observation`) — `clarification_feedback` is retired, so the 30+ entries
threshold no longer applies.
