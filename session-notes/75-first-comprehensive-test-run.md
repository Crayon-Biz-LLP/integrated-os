# Session 75 — First Comprehensive Test Run (2026-08-15)

The first end-to-end run of the full suite (fast + live nightly tiers) after the
Phase 0–4 build (`70719b8`). Purpose: prove the gates actually work on a real
run, and capture every failure/flake for the record.

## What was run

| Chunk | Command shape | Result |
|---|---|---|
| Fast tier (L0 ruff + residue + marker lint + mock pytest + 62 Flutter tests) | `scripts/run_tests.py fast` | ✅ GREEN |
| unit + top-level integration (live) | `LIVE_DB=true pytest tests/unit tests/test_*.py` | ✅ 840 passed, 4 skipped |
| sim suites (live) | `LIVE_DB=true pytest tests/sim` | ✅ 83 passed, 14 skipped (1 flake, see below) |
| clusters (live) | `LIVE_DB=true pytest tests/clusters` (2 batches) | ✅ 38 passed, 1 xfailed |
| tenants + golden (live) | `LIVE_DB=true pytest tests/tenants tests/golden` | ✅ 4 passed, 3 skipped |
| UAT L4 — 23 scenarios (live, real LLM) | `LIVE_DB=true pytest tests/uat` (3 node-id batches) | ✅ 22 passed · **1 flake: S2** |

The full live run had to be chunked because the tool's per-command cap is 600s
and the nightly budget is 20 min; the UAT harness is a single parametrized test
so it was selected by exact node id (`test_scenario[S2]`, …).

## Bugs & flakes found

### 1. CI fast tier red — 4 independent root causes (fixed & committed `097906b`)
The CI log the user pasted showed 49 pytest failures + 2 golden failures:
- **pytest-env missing from `requirements.txt`** — `pytest.ini`'s dummy-cred
  `env` block was ignored on CI, so the real Supabase secrets ran the live
  sim/clusters suites against the shared Test tenant. (THE big one.)
- **Real Supabase secrets were injected into the fast job** in `test.yml` —
  fast must be DB-free by invariant.
- **`test_url_shortcut.py` poisoned the DB singleton at import time** — MagicMock
  leaked into every suite (`Invalid transition '<MagicMock>'` failures).
- **`test_migrations_replay` needed pgvector** the runner lacked (now guarded +
  installed in the nightly workflow).
- **Both goldens rendered emoji via the host font** (Noto Color Emoji on Linux
  CI) instead of Ahem. Probes proved macOS renders everything as Ahem boxes;
  the goldens now force `fontFamilyFallback: ['FlutterTest']` + ASCII-only
  fixtures (the widget's hardcoded 📋/☀️ glyphs are pinned via the surface
  DefaultTextStyle). PNGs regenerated.
  - Correction to an earlier diagnosis: emoji were initially blamed as colored
    on macOS; pixel probes showed they are already monochrome there. The CI
    diff is Linux-only host-font resolution. The fix (explicit Ahem fallback)
    is correct regardless.

### 2. `test_s6_bot_receipts_stripped_from_context` — owner-chat isolation gap
Failed once in a full sim run; passed solo, at module level, and on re-run.
Root cause: the module used `TELEGRAM_CHAT_ID` (the owner chat) instead of a
per-run chat — rows land outside the X4 band and marker sweeps, so a killed
run's residue can bleed into the next session. **Fixed**: all 7 tests now use
`run_chat_id()` from the X4 per-run band + `patch.dict(TELEGRAM_CHAT_ID)` per
call (test_s7 pattern). Module verified 7 passed.

### 3. `test_scenario[S2]` (L4, entity resolution) — accumulation + LLM variance
Failed in the S1–S8 batch and again on a clean DB; passed on re-runs.
Two layers:
- **Stale-residue accumulation (fixed):** S2's entity `TestOrg Beta` was
  unprefixed, so clean-slate and marker sweeps couldn't see it. 12 stale rows
  (5 graph_nodes incl. a full-sentence label, 2 Amita tasks, 5 raw_dumps) had
  accumulated from earlier runs — including one from a killed run at 15:25.
  **Cleaned** (owner-scoped to the Test tenant; a `supersedes_id` FK chain was
  nulled before delete) and **marker-prefixed** the entity text in S2/S8/S10
  (`{PREFIX} TestOrg Beta`), so every created node is swept + leak-guarded.
- **Residual LLM/planner nondeterminism (documented, not fixed):** `TASK`
  routing runs the real Gemini planner (`plan_actions`). The failure mode was
  "intent=TASK, confidence=1.0, yet no task AND no batch workflow created" —
  the planner returned nothing. This is inherent to L4 (real LLM by design);
  the scenario flips pass/fail run to run. No DB state was involved.

### 4. `RuntimeWarning: coroutine ... was never awaited` (memory.py:36)
`tests/test_memory_wiring.py` mocked the SYNC `schedule_index_memory` with an
`AsyncMock`, so the production call site produced an un-awaited coroutine.
**Fixed**: `AsyncMock` → `MagicMock` (test-only, one line). Verified with
`-W error::RuntimeWarning`.

## Operational notes (from running the suite)

- **Killed runs leave a stale Redis sandbox lock** (45-min TTL). The runner's
  next live session fails fast with `SandboxLockHeldError`. Clear it only when
  the token's hostname matches your machine:
  `client.delete('rhodey:test-sandbox:lock')` (see `tests/fixtures/run_isolation.py`).
- **`nohup`/`setsid` do not survive the tool's shell teardown** — long runs
  must be chunked under the 600s cap; the runner's own tiers already respect
  the budgets (fast 5 min, nightly 20 min).
- **Live runs need `LIVE_DB=true`**; conftest then loads real creds from `.env`
  with override (pytest-env's dummies are applied first).

## Open / deferred

- **L4 real-LLM flakiness:** S2 (and any real-LLM scenario) can flip pass/fail
  with provider variance/rate limits. Options: nightly rerun policy,
  tier-2 classification, or mocking the planner for determinism. Not decided.
- **Cleanup of stale `projects`/`organizations` test rows** beyond TestOrg Beta
  (the X1 ledger kept `projects`; any other unprefixed test entities should
  follow the marker-prefix rule).
- Commits pending for this session's fixes: `test_memory_wiring.py`,
  `run_uat.py` (marker prefixes), `test_thread_classification.py` (chat band),
  `AGENTS.md` (user operating rule), this note.

## Rule for future runs

Any test that creates persistent state (tasks, graph_nodes, memories,
workflows) must carry a marker prefix (`[TEST]`/`[SIM_TEST]`/`[UAT]`) or a
per-run chat/thread id from the X4 band — otherwise the clean-slate, leak
guard, and owner-scoped cleanups cannot see it and it accumulates across runs.
