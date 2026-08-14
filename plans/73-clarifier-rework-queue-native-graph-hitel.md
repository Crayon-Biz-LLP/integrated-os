# 73 — Clarifier Rework: Queue-Native Graph HITL

**Status:** Implemented 2026-08-14 (phases 0-5 landed; verification: ruff clean, 622 unit tests pass, flutter analyze clean. Entities "?" marker deferred per phase 6.)
**Canonical vision:** `product-summary/00-vision-and-mindset.md`
**Architecture home:** `product-summary/99-architecture-reference.md` (Layer 3 Intelligence — Knowledge Graph HITL; Layer 4 Presentation — Pulse/Decision Pulse; Layer 5 Persistence — state machines, pending tables)

---

## 1. Decision record

The graph clarification loop (`core/clarifier.py`) currently hijacks pending edges/nodes out of the
sanctioned HITL flow (`pending_graph_edges → Quick Confirmation → approval`), flips them to
`awaiting_clarification`, and asks the user via a Telegram-only question surface that (a) fires on
**every** LLM-extracted edge (hardcoded confidence 0.5 < 0.7 threshold), (b) arrives 30+ minutes
late (batch dispatch on the sentinel heartbeat), and (c) is invisible in the app's Quick
Confirmation queue — the exact "inverted labor" anti-pattern from the vision doc.

**Decisions:**
1. **Kill the question flow.** `evaluate_node`/`evaluate_edge` stop generating questions; pending
   items stay in the queue where they already belong. The queue already surfaces new people (with
   context sheet), edges (with confidence badge + edit), and merges.
2. **Keep gated knowledge.** The HITL gate itself is the architecture's documented edge flow — it
   stays. The clarifier's *contradiction check* is repurposed as a **card hint** on the queue.
3. **Silent gate for noise.** Low-confidence, never-acted-on edges expire via an extension of the
   existing `decision_pulse` sweep; re-mentioned edges get a confidence bump at extraction
   (corroboration). No new tables, jobs, or fire-and-forget tasks.
4. **One daily check-in.** The pulse briefing appends at most one line listing newly tracked
   unconfirmed items (the "transparency report" append pattern).
5. **Learning loop closes.** Queue approvals and in-place Entities corrections emit observations.
6. **Deferred (recorded, not forgotten):** the Entities "?" unconfirmed marker.

**Key verified facts this plan rests on:**
- `evaluate_node`/`evaluate_edge` are the only generators; everything hangs off their return values
  (`core/pulse/graph.py:1577,1725` gate `store_and_send_clarification` on a non-None return).
- `PENDING_GRAPH_EDGES_STATUSES` (state_machines.py) contains **only** `pending/approved/rejected` —
  the clarifier's edge flip to `awaiting_clarification` was already an ungoverned status (state-machine
  violation). Removing the flip is a hardening, not a regression.
- `backfill_graph.py` calls `evaluate_node` fire-and-forget; `tests/unit/test_backfill_graph.py`
  monkeypatches it — signatures must be preserved or the call sites removed cleanly.
- The app already renders everything needed; `DecisionType.clarification` goes dormant (no UI build).
- The repo requires doc updates with code (CHANGELOG + 4W1H commit enforcement). Docs are Phase 0.

---

## 2. Layer map (definition of done — every change must fill a row)

| # | Change | Layer | File(s) | Sanctioned entry point | Doc update |
|---|--------|-------|---------|------------------------|------------|
| 3.1 | Stop question generation | 3 Intelligence | `core/clarifier.py`, `core/pulse/graph.py` | Existing extraction hooks (`insert_extracted_entities`) | 28, 99-arch |
| 3.2 | Remove dispatch machinery | 4 Presentation | `core/pulse/sentinel.py` | Sentinel piggyback removal | 28, 99-arch |
| 3.3 | Remove answer surface | 4 Presentation / Integration | `api/index.py` (`/api/clarification`), `core/webhook/handler.py` (c-shortcode), `rhodey_app` (`submitClarification`) | Endpoint + handler removal | 28, 99-arch |
| 3.4 | Legacy data migration | 5 Persistence | `db/73_*.sql` | Numbered migration | 99-arch |
| 3.5 | State-machine adjustments | 5 Persistence | `core/lib/state_machines.py` | Formal status/transition definitions | 99-arch |
| 3.6 | Silent gate — expiry | 4 Presentation | `core/pulse/decision_pulse.py` | Extension of existing FYI-expiry + `awaiting_details` revert sweep | 28, 99-arch |
| 3.7 | Silent gate — corroboration | 3 Intelligence | `core/pulse/graph.py` (`insert_extracted_entities`) | Extension of existing dedupe/skip path | 28 |
| 3.8 | Contradiction card hint | 3 + 4 | `core/pulse/graph.py` (helper), `api/index.py` (feeds), `rhodey_app` (card) | Feed enrichment + existing card render | 28 |
| 3.9 | Briefing check-in line | 4 Presentation | `core/pulse/briefing.py` | Post-generation append (transparency-report pattern) | 28 |
| 3.10 | Learning loop — observations | 3 + 4 | `core/pulse/graph.py`, `api/index.py` (Entities routes) | Existing `emit_observation` pattern | 28 |
| 3.11 | Tests | — | `tests/unit/…` | Existing pytest suite | — |
| 3.12 | Docs | — | `28`, `99-architecture-reference`, `README` index, `CHANGELOG` | Doc lifecycle convention | — |

**Rule:** if a change cannot fill this table, it does not ship.

---

## 3. Phases (each independently verifiable)

### Phase 0 — Docs first (same commit as the code it describes)

| File | Change |
|---|---|
| `product-summary/28-clarification-loop-guards.md` | Rewrite the "Clarification Loop Architecture (Phase 2)" section: mark Telegram-dispatch superseded; restate the 6-function interface as removed/silent; document the new model (queue-native HITL, card contradiction hint, briefing check-in, silent gate). |
| `product-summary/99-architecture-reference.md` | Layer 3 key files: restate `core/clarifier.py` role or remove; Layer 5 DB-backed state: drop `clarification_feedback`/`pending_graph_clarifications`; note `awaiting_clarification` removed from the edge flow; add changelog row. |
| `product-summary/README.md` | Update the doc-28 description line if it names the Telegram loop. |
| `CHANGELOG.md` | Log the doc restructure + feature change. |

**Verify:** `git diff --stat` shows docs in the same commit as each code phase; doc text no longer
claims the Telegram question loop is active.

### Phase 1 — Kill the question flow (smallest, highest-value change)

1. `core/clarifier.py`:
   - `evaluate_node` / `evaluate_edge` → return `None` with a docstring pointing at the new model
     (signatures preserved — `backfill_graph.py` and `tests/unit/test_backfill_graph.py` import them).
   - Delete `store_and_send_clarification`, `build_batch`, `handle_response`, `next_shortcode`,
     `dedupe_batch` and their call sites/imports.
   - Keep nothing else. If a caller still imports these, the plan fails the layer map — remove the
     caller instead.
2. `core/pulse/graph.py`: remove the `if clar:` dispatch blocks at the node hook (~1576-1581) and
   edge hook (~1724-1736). **Preserve the contradiction check logic** — it moves to the Phase 4 helper
   (3.8) rather than being deleted.
3. `core/skills/backfill_graph.py`: remove the two `evaluate_node(...)` hook calls (~390, ~461).
4. `core/pulse/sentinel.py`: remove the 5-min clarification-dispatch piggyback (~380-400) and the
   weekly-sweep "unanswered clarification(s)" line (~315-322).
5. `api/index.py`: remove `/api/clarification` route (~3438).
6. `core/webhook/handler.py`: remove the c-shortcode → `handle_response` block (~753-762).
7. `rhodey_app/lib/services/api_service.dart`: remove `submitClarification` (now dead).

**Verify:** `rg "clarification_feedback|handle_response|build_batch|store_and_send"` under `core/`
`api/` `rhodey_app/` returns no live callers (table reads in the migration + web dashboard noted in
8). `python3 -m pytest tests/unit/test_backfill_graph.py tests/unit/test_insert_extracted_entities.py -q`
green.

### Phase 2 — Legacy migration + state machine (5 Persistence)

1. `db/73_*.sql` (new numbered migration):
   - `UPDATE pending_nodes SET status='pending' WHERE status='awaiting_clarification';`
   - `UPDATE pending_graph_edges SET status='pending' WHERE status='awaiting_clarification';`
   - Resolve open rows: `UPDATE clarification_feedback SET resolved_at=now() WHERE resolved_at IS NULL;`
     (resolution preserves history; deletion is acceptable if history isn't wanted — decide in review).
2. `core/lib/state_machines.py`:
   - `PENDING_NODES_TRANSITIONS`: remove `"awaiting_clarification"` from the `pending` → … set; drop
     the `awaiting_clarification` transition row (keep the status constant only if legacy reads need
     it — after the migration, remove both).
   - `PENDING_GRAPH_EDGES_STATUSES`: add `"expired"`; `PENDING_GRAPH_EDGES_TRANSITIONS`: add
     `pending → expired` (formal authority for the Phase 3 gate).
3. `rhodey_app/lib/services/api_service.dart`: the `_parseGraphNodes` `awaiting_clarification` branch
   (~874-879) becomes dead — leave the `awaiting_details` branch (person-context flow is separate and
   stays), delete the clarification branch.
4. Web dashboard (`frontend/`): the decisions-page clarification sections become inert — note as a
   low-priority cleanup item (app-primary; not required for v1).

**Verify:** migration runs idempotently against a copy; `guard_is_valid_status("pending_graph_edges",
"expired")` is True and `guard_is_valid_transition("pending_graph_edges","pending","expired")` is
True; no code path can still write `awaiting_clarification` (state machine is the authority).

### Phase 3 — Silent gate (extend existing sweeps only)

1. `core/pulse/decision_pulse.py` — extend the existing expiry block (~90-114, which already expires
   stale FYI items and reverts stale `awaiting_details`):
   - Add: mark `pending_graph_edges` rows `expired` where `status='pending'` AND
     `confidence < 0.6` AND `created_at < cutoff` (cutoff matching the FYI window, e.g. 14 days).
   - Same guard/audit-log style as the sibling blocks. One named, bounded query; no new cron.
2. `core/pulse/graph.py` — `insert_extracted_entities` dedupe path (the existing permanent-edge skip
   at ~1695-1710): on re-mention of a pair that already has a `pending` edge, bump its `confidence`
   (corroboration) instead of inserting a duplicate. Bumped edges rise above the gate threshold and
   surface normally.

**Verify:** a unit test proves (a) a low-confidence stale edge is marked `expired` via
`guard_is_valid_transition`, (b) a re-mentioned pair bumps confidence and is not duplicated. No new
table/job/fire-and-forget introduced (layer map holds).

### Phase 4 — Card hint + briefing check-in (the value-add)

1. **Contradiction hint** — `core/pulse/graph.py` (or `core/lib/graph_rules.py`): extract the
   contradiction logic from `evaluate_edge` into a helper, e.g.
   `enrich_pending_edges_with_conflicts(rows) -> rows` (for each pending edge, query `graph_edges`
   for the same node pair with a different relationship; attach `conflict_with`). Wire into:
   - `api/index.py` `/api/inbox` `_edges()` feed builder (~605-618) and `/api/pending-graph-edges`.
   - `rhodey_app`: carry `conflict_with` through `_parseGraphEdges` metadata; render a
     "⚠️ conflicts with existing KNOWS edge" line on the edge card (`decision_card.dart`).
2. **Briefing line** — `core/pulse/briefing.py`: after briefing generation, append at most one line
   (the existing transparency-report append at ~1250-1270 is the sanctioned pattern) listing
   newly-tracked unconfirmed items (pending edges below the gate threshold / recently added).
   Empty when nothing is unconfirmed — "all clear" stays a feature.

**Verify:** feed unit tests assert `conflict_with` is present/absent correctly; a live TestClient
check of `/api/inbox` shows the field; briefing test asserts the line appears only when unconfirmed
items exist.

### Phase 5 — Learning loop (observations)

1. `core/pulse/graph.py`: edge approval already emits (`emit_observation` at ~905). **Verify** node
   approval emits too (pattern exists in the edge path — add if missing).
2. `api/index.py` Entities routes (rename ~3600s, type-change ~3673s, delete ~3901s): emit
   `emit_observation(subsystem='entity_extraction', event_type='correction', outcome='corrected',
   features=…)` — in-place corrections train the system, per the vision's "every decision must
   persist and train."

**Verify:** a unit test asserts node-approval emits; the Entities correction routes emit
observations on the mocked client.

### Phase 6 — Deferred (recorded)

| Item | Why deferred | Sanctioned path when built |
|---|---|---|
| Entities "?" unconfirmed marker | New surface; needs the most plumbing | "Unconfirmed" already exists as `pending_*` rows + `confidence`; marker is a Layer 4 view over existing endpoints (`/api/pending-graph-edges`, `/api/pending-graph-nodes`); only if items must outlive expiry would a state-machine status be added — still sanctioned. |

---

## 4. Risks & guards

- **Over-deletion:** `core/lib/clarification_state.py` is the DB-backed state for the *NLP
  correction sessions* (person-context / edge-edit state machines in `handler.py`) — **that is a
  different feature and stays.** Delete only the graph-question machinery named in Phase 1.
- **Docs drift:** Phase 0 is first; each code phase lands with its doc rows in the same commit.
- **Gate scope creep:** Phase 3 covers edges only. Node noise is handled by the existing person-context
  flow; extending the gate to nodes is a future decision, not v1.
- **Grounding-approval gap (pre-existing):** `handle_response` never created the node for approved
  grounding clarifications. The question flow's removal makes this moot (nodes resolve via
  `process_graph_pending_decision`, which creates nodes). No action needed — document in the migration
  notes so it isn't rediscovered.

---

## 5. Test & verification plan

- Baseline (already green, 44 tests): `test_insert_extracted_entities`, `test_graph_pipeline`,
  `test_inbox_feed`, `test_batch_concurrency`, `test_backfill_graph`, `test_workflow_clarification`,
  `test_mentions_provenance`.
- New tests: feed `conflict_with` enrichment; gate expiry (state-machine-valid); corroboration bump;
  node-approval observation; briefing line presence/absence.
- `ruff check .`, `python3 -m pytest tests/unit -q`, `flutter analyze`, live TestClient check of
  `/api/inbox` shape (nothing real resolved).
