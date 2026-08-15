# Session 73 — Comprehensive Test Suite Build + Ledger X1–X5

**Date:** Aug 15, 2026 · **Commit:** `70719b8` (test: comprehensive suite — phases 0-4, aspects/runner/CI, UAT-as-L4, ledger X1-X5)

## What was built

**The comprehensive test suite (plans/75-comprehensive-test-plan.md, v2.16):**
- **Phase 0–1:** inventory doc, 13 aspect markers + strict registration, ~94 modules tagged, marker-presence lint (`scripts/check_marker_presence.py`), unified runner (`scripts/run_tests.py`) + Makefile + pre-push hook, CI fast/nightly split, `frozen_clock` fixture.
- **Phase 2:** 22 UAT scenarios absorbed as L4, webhook `TEST_CHAT_IDS` allow-list (default-off) + auth negatives, owner-scoped cleanup, fail-closed cross-tenant leak guard.
- **#1–#6:** migration replay (94/94), webhook auth negatives, learning-loop tests (caught the undo-training trust-breaker), calendar/email/sync/push orchestration (42 tests), boundary-clock matrix (40), golden re-base, API contract (pinned 78-path/89-op; fixed 8 duplicate operationIds), health-as-L3, security negatives (17), Flutter goldens (2 PNGs), on-device integration tests (Pixel 8a emulator).
- **X6/X7/X8:** teardown batching (FK-safe parallel tiers), per-layer cov floors, calendar boundaries, on-device onboarding test.

**Ledger X1–X5 (deferred from earlier waves, closed here):**
- **X1 — `projects` table: DECIDED keep.** Live audit: 37 real dormant rows, zero writers, load-bearing for test harnesses (live parent in FK-orphan sweeps + seeds) and `graph_nodes.db_record_id` references it.
- **X2 — graph/edge undo-training.** `record_decision()` gained a `metadata` param; all 4 graph decision sites (node/edge approve/reject in `core/pulse/graph.py`) persist `learn_features` = exact decision-time feature dict + `learn_subsystem='entity_extraction'`; graph undos now demote the right pattern via the already-wired `emit_undo_correction`.
- **X3 — confirm honesty.** New `emit_confirmed_observation()` (`core/webhook/utils.py`) — per-item confirmations against each decision's real subsystem + decision-time features; Telegram `confirm_auto_all` + `/api/auto-decisions/confirm` switched; audit log reports the trained count ("patterns strengthened" no longer an overclaim).
- **X4 — per-run chat allocation + Redis lock.** `tests/fixtures/run_isolation.py`: per-process chat band (9.1M–9.99M) + thread UUIDs; cross-machine `acquire_sandbox_lock()` (Upstash `SET NX EX`, TTL 45 min, fail-closed on second run) closes the last documented race (nightly cron vs local). Verified live.
- **X5 — clean-slate pre-delete.** Session-start purge of test-tenant marker rows (owner-scoped, children-first) + chat/thread-keyed rows; verified live (purged 15 residual `[TEST]` graph_nodes).

## Bugs caught & fixed (the suite earned its keep)
- Onboarding PageView bug — Sign-in unreachable on a real device (integration test).
- 8 duplicate OpenAPI operationIds.
- Undo-training trust-breaker (decisions weren't training on undo).
- Migration-chain breakage + non-self-contained migration.
- `get_google_calendar_events` date crash.
- Stale planner golden.
- **Leak-guard `raw_dumps.text` blind spot** — column is `content`; the guard could never flag raw_dumps leaks. Fixed in guard + pre-delete.

## Final state
865 pytest (854 baseline + 11 new) + 62 Flutter tests · ruff + marker lint clean (94 modules) · frontend `tsc --noEmit` clean · live lock + pre-delete smoke-tested.

## Parking lot
- Dashboard (D3) — user's call, not touched.
- Flaky-quarantine mechanism (§15) — documented policy, zero flakes exist, build on demand.
