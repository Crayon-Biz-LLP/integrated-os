# 71. Decisions Ledger & Learning Loop

> Verified against code 2026-08-15 (ledger work X2/X3 of the test-suite session).
> This is vision criterion #4 made concrete: **every user decision — approve /
> reject / snooze / confirm / undo — persists AND trains.** A "Not now" that
> silently resets is a trust-breaker; this system never does that.

## The `decisions` table

Every decision lands in `decisions` (see `05-database-schema.md`):

- `decision_type` — `approve`, `reject`, `snooze`, `confirm`, `undo`, …
- `source` / `source_ref` — where it came from (`telegram`, `api`, `app`,
  `pulse`) and what record it acted on (`table:id`).
- `entity_id` / `entity_type` — graph context of the decision.
- `confidence` / `rationale` / `context` — model confidence and the
  decision-time situation.
- **`metadata.learn_features`** — the exact decision-time feature dict
  (subsystem, source, entity, signal features). This is what makes the loop
  trainable: the *same features* used to predict are the ones corrected.
- `auto_decided`, `reversible`, `superseded_by` (undo chain), `expires_at`,
  `decided_at` / `verified_at`.

## The learning loop

```
Decision (approve/reject/snooze/confirm/undo)
   → record_decision(...) persists to `decisions`
   → subsystem pattern update:
        correct/incorrect → subsystem_patterns
        soft acceptance   → soft_accepted_count
        operator endorse  → operator_endorsed_count
   → prediction telemetry: subsystem_telemetry (predicted vs actual)
```

- **`core/decisions.py`** — `record_decision()` (with the `metadata` /
  `learn_features` payload), plus the correction emitters:
  - **`emit_undo_correction()`** — an undo *demotes* the pattern that produced
    the decision (the graph-undo path already wired: graph/edge undo demotes the
    right pattern — X2).
  - **`emit_confirmed_observation()`** (`core/webhook/utils.py`) — per-item
    confirmations against each decision's **real subsystem + decision-time
    features**. Both bulk paths use it: Telegram `confirm_auto_all`
    (`core/webhook/handler.py`) and `POST /api/auto-decisions/confirm`
    (`api/index.py`). The audit log now reports the trained count — "patterns
    strengthened" is no longer an overclaim (X3).
- **`subsystem_patterns`** — per-subsystem learned patterns keyed by
  `feature_hash` with confidence and correct/corrected/soft-accepted/
  operator-endorsed counters.
- **`subsystem_telemetry`** — prediction/outcome pairs (features, predicted,
  actual, outcome, latency) that drive eval and future confidence.

## Graph decisions are first-class

All **4 graph decision sites** (`core/pulse/graph.py` — node approve/reject,
edge approve/reject) persist `learn_features` = the exact decision-time feature
dict with `learn_subsystem='entity_extraction'`. So entity-extraction quality
improves from every graph approval/rejection, and the frontend's verify/reject
routes prefer `metadata.learn_features` (falling back to source→subsystem
mapping for pre-fix decisions).

## Testing the loop

`tests/unit/test_learning_ledger.py` (+7 tests) covers record-with-features,
undo demotion, confirm-honesty payloads, and the Redis sandbox lock.
`tests/unit/test_learning_loop.py` covers the decision → persist → behavior
delta (the loop's end-to-end promise).
