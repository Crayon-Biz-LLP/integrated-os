# 75 — Comprehensive Test Plan (v2.17)

## Changelog
- v2.17: **Nightly pipeline honesty + fault-injection proof (Phase 1) + api floor calibration.**
  (1) **The nightly was SILENTLY RED** — `nightly.yml` ran the runner
  through `| tail -60` without `set -o pipefail`, so the step always exited
  0 and the job reported success while `nightly tier: FAILED` sat in the
  log. The pipe is removed (full output streams to the run log) and
  `pipefail` stays as a guard; `notify_failure` becomes live again.
  (2) **Fault-injection pass — the suite bites (5/5).** Re-injected each
  real production bug one at a time and required the suite to go RED:
  debounce-sentinel revert → fresh-boot regression test · planner title
  backstop removal → 3 title-injection tests · golden comparator wrong-
  platform routing → both goldens (original 2.36%/0.83% pixel signatures)
  · rate limiter disabled → test_sliding_window_fallback · tenant-1 token
  planted in shared code → residue gate (exit 1, exact file+line). All
  reverted; tree byte-clean. Caveat: DB-level RLS isolation is
  database-enforced (migration-level injection would be the deeper proof —
  parked). (3) **api coverage floor calibrated** — the v2.14 "floors
  closed" claim was incomplete: api measured 12% (api/index.py 12%,
  api/briefing.py 10%) against the unvalidated default 20. Set
  `API_COV_FLOOR=10` (measured-baseline pattern, like core 23→20) with the
  measurement documented in nightly.yml; closing the real gap is §16 D6.
  (4) **Open:** the CI nightly completes in ~60s vs ~14min locally — live
  suites appear to SKIP on CI. After the pipefix lands, dispatch and verify
  per-layer counts (does the deep tier actually run?).
- v2.16: **X4 residual CLOSED — cross-machine sandbox lock.** The one
  remaining race (marker-title sweeps crossing truly-concurrent runs, e.g.
  nightly cron vs a local live run) is now structurally impossible:
  `acquire_sandbox_lock()` (tests/fixtures/run_isolation.py) takes a Redis
  `SET NX EX` lock via the already-wired Upstash client before the live
  session — second run fails fast with holder info (bounded wait, default
  60s, env `SANDBOX_LOCK_WAIT_S`), TTL self-expires on a killed run
  (default 45 min, env `SANDBOX_LOCK_TTL_S`), release never clears another
  run's lock, Redis-unconfigured envs skip it. Session fixture order:
  acquire → clean-slate pre-delete (X5) → run → release. Verified live:
  held→fail-closed→release→reacquire. +4 unit tests (11 in
  test_learning_ledger.py).
- v2.15: **Deferred ledger X2–X5 closed, X1 decided.** (1) **X2 graph/edge
  undo-training CLOSED** — `record_decision` gains a `metadata` param; the 4
  graph decision sites (node/edge approve/reject in core/pulse/graph.py)
  now persist `learn_features` (EXACT decision-time feature dict) +
  `learn_subsystem='entity_extraction'`, so the already-wired
  `emit_undo_correction` demotes the right pattern on graph/edge undo.
  (2) **X3 confirm honesty CLOSED** — the two bulk confirm paths (Telegram
  `confirm_auto_all`, API `/api/auto-decisions/confirm`) no longer emit a
  decorative `auto_decisions` observation: new `emit_confirmed_observation`
  emits PER-ITEM confirmations against each decision's real subsystem +
  decision-time features; frontend verify/reject routes prefer
  `metadata.learn_features` the same way. "patterns strengthened" is no
  longer an overclaim. (3) **X4 per-run chat allocation CLOSED** — new
  `tests/fixtures/run_isolation.py` draws a per-process chat band
  (9.1M–9.99M) + per-run thread UUIDs; sim seed, note_capture, suite2, UAT
  all use it; leak guard knows the band + legacy ids. (4) **X5 clean-slate
  pre-delete CLOSED** — session-start purge of test-tenant marker rows
  (owner-scoped, children-first; non-test-tenant rows deliberately left for
  the fail-closed guard). Verified live: purged 15 residual `[TEST]`
  graph_nodes. **Bonus fix: the leak guard had a blind spot** —
  `raw_dumps.text` doesn't exist (it's `content`), so raw_dumps leaks were
  never flaggable; corrected in the guard + pre-delete.
  (5) **X1 projects DECIDED: leave as-is** (see §19).
- v2.14: **Phase-1 leftovers executed.** (1) **X6 teardown batching CLOSED** —
  the sim suite's per-test teardown ran ~17 sequential network deletes + 3
  FK-orphan passes per test × 97 tests (the "teardown dominates" finding in
  docs/test-inventory.md §4, 2–11s/test). Rewritten in tests/sim/conftest.py
  as FK-safe parallel tiers: tables grouped children-before-parents
  (verified against the live FK graph — every edge among swept tables is
  SET NULL or CASCADE except org_creation_signals.task_id/raw_dump_id which
  are NO ACTION and therefore pinned to tier 0), each tier's deletes run
  concurrently via ThreadPoolExecutor (each delete still owner-scoped
  `eq('owner_id', TEST_TENANT_UID)` — leak-safety unchanged), tiers
  sequential. Module-load sweep + per-test cleanup + FK-orphan passes all
  batched the same way. (2) **Per-layer coverage floors** — §11 called for a
  floor per layer but the runner had ONE global floor. Now the single
  pytest-cov measurement is enforced per source layer via `coverage report
  --include` (instant, no extra pytest runs — avoids the 13× --cov runtime
  §11 warns about): `core/*` under COV_FLOOR and `api/*` under
  API_COV_FLOOR (both default 20, env-configurable, fail-closed when a
  layer shows 0%). A suite that stops exercising the core layer can no
  longer be hidden by API coverage and vice versa. Remaining by design:
  flaky-quarantine mechanism (§15) is policy-on-demand (no flaky tests to
  quarantine yet); dashboard parked (D3).
- v2.13: **X8 closed — Flutter integration_test landed on a real emulator.**
  With the Pixel 8a AVD available (launched headless: `-memory 1536` — the
  default 4GB config exceeds this host's free RAM and the process was being
  killed), `rhodey_app/integration_test/app_flow_test.dart` (2 tests) drives
  the REAL app binary on-device: fresh-install boot → onboarding welcome
  (all 5 persona cards), persona pick → Continue → Sign-in step, Back
  navigation. `integration_test` added to dev_dependencies; real-time boot
  polling (`runAsync` + pump loop — the app's `main()` awaits real
  secure-storage/prefs init before `runApp`, so `pumpAndSettle` alone
  returns too early). **While building it, a real production bug was found
  and fixed**: the onboarding PageView was being **recreated mid-animation**
  whenever `_page` became > 0 (the progress bar / bottom bar slots change
  type between pages, and with no key Flutter's reconciliation destroyed and
  re-created the PageView element — cancelling `animateToPage` and snapping
  back to page 0, making the Sign-in step unreachable on device). Fixed with
  a stable `ValueKey('onboarding-pages')` on the Expanded + an always-rendered
  progress-bar slot; regression-pinned in `test/onboarding_nav_test.dart` (2
  tests incl. an element-identity assertion) + a minimal
  `test/pageview_animation_test.dart`. App suite now **62 tests**. Run via
  `flutter test integration_test` on a booted emulator; wired as `make
  test-app-integration`.
- v2.12: **Phase 4 (CI evolve) executed.** The push gate (test.yml) is now
  the fast tier through the unified runner — `-c /dev/null` is GONE (that
  flag discarded pytest.ini entirely, so CI respected no markers/skips).
  Fast = L0 (ruff + residue + marker lint) + L1/L2-mock + **app**: Flutter
  set up in CI (`subosito/flutter-action@v2`, version-pinned 3.44.5 to keep
  goldens SDK-stable) + `flutter pub get`, so the app's 58 tests incl. the
  briefing-card/task-ack goldens now gate every push (previously skipped —
  flutter wasn't on PATH). Nightly (nightly.yml) already runs
  `run_tests.py nightly --live --coverage` with `TEST_CHAT_IDS` for the L4
  UAT scenarios; runner comment updated (L4 no longer "arrives in Phase 2" —
  it runs inside the live pytest session, migration replay included via
  tests/ root, postgres auto-discovered on ubuntu). Remaining by design:
  flaky-quarantine mechanism (§15) is policy-on-demand (no flaky tests to
  quarantine yet).
- v2.11: Phase-3 #6 tail (Flutter goldens) executed. **Golden snapshot
  tests** landed in the app (`rhodey_app/test/goldens/`): the briefing card
  in the exact Aug-10 shape that broke (bold `**Work**` headers, bullets,
  emoji → body rendered twice + literal asterisks) + the task-ack card —
  pinned as committed PNGs via `matchesGoldenFile`. Any layout/rendering
  change to these cards is now a reviewed golden update
  (`make test-app-goldens` regenerates), never a silent regression — the
  §10 "reviewed like code" policy now covers the app's visual contract.
  App suite: **58 tests** (56 widget + 2 goldens), all green; `make
  test-app` alias added (the runner's fast tier already invokes `flutter
  test`). **integration_test deferred to ledger X8** — it requires a
  device/emulator (none on the dev machine: `flutter devices` empty) and
  real-font/API rendering, a separate runtime concern from the hermetic
  widget suite.
- v2.10: Phase-3 #6 tail (health-as-L3 mapping + security negatives) executed.
  **Health/validate mapped, not duplicated** (§14.2): health.yml →
  `scripts/run_health.py --force` → `run_full_health_check()` (scheduled +
  dispatch + Telegram alert) and validate_deployment.yml (post-CI Modal
  validation) ARE the L3 health surface — recorded as covered-by-workflow in
  §4, no parallel representation built. Landed `tests/unit/test_health_wrapper.py`
  (8, ops-exempt) pinning the testable wrapper behavior the workflows rely
  on: `is_business_hours()` boundary matrix (UTC 03:00–17:00 = IST 08:30–22:30),
  CLI exit-code/alert contract (skip outside hours, silent-when-clear,
  issues → Telegram alert + exit 1, `--force` bypass), and the M6 tenant
  fan-out failure isolation (one tenant's health failure becomes an issue,
  never aborts the others). **Security negatives** landed
  `tests/unit/test_auth_negatives.py` (17, auth): OTP brute-force cap
  (5 attempts, burn-one-try increments BEFORE validation, no burn at the
  cap), 60s resend rate limit, expiry, consumed-code rejection, the
  ANTI-ENUMERATION parity rule (unknown email / no code / wrong code /
  reused code all return the identical message), daily send cap,
  disabled-user no-email; API-key negatives: unknown key → None (incl.
  table-missing fail-closed), `require_api_auth` fail-closed 503 when
  `API_SECRET_KEY` unset + no dev auth, 401 on wrong key, per-user key
  tenant scoping, disabled-user key never scopes.
- v2.9: Phase-3 #6 (API contract / OpenAPI) executed. **Found + fixed a
  real spec-invalidity bug**: `app.openapi()` produced **8 duplicate
  operationIds** — the `@app.api_route(path, methods=["GET","POST"])`
  pattern (pulse-cron, sentinel, decision-pulse, maintenance, health,
  admin/spend, roundup, beeper-sync) yields ONE route object with both
  methods, and FastAPI's `generate_unique_id` uses `list(route.methods)[0]`
  → GET and POST share one operationId → invalid OpenAPI for strict
  consumers (SDK generators, API tooling). Fix: split each into explicit
  `@app.get` + `@app.post` decorators (routing behavior identical — verified
  by the route-inventory test + existing API tests). Landed
  `tests/unit/test_api_contract.py` (ops surface, exempt from aspect lint):
  **pinned route inventory** — the exact 78-path/89-operation surface,
  exact-match against the live app (adding/removing a route is now a
  deliberate, reviewed pin change, not a silent break) + **spec validity**
  (OpenAPI 3.x, title/version, unique operationIds, every route documented).
  Result: valid OpenAPI 3.1.0, 0 duplicates.
- v2.8: Golden re-base executed. **Finding that reshaped §10**: the
  `*_tenant1.txt` files are NOT Test-tenant data to migrate away from — they
  are channel-tenant (tenant #1) regression pins BY DESIGN (Danny's pinned
  output shape, compared HERMETICALLY via mocked rows/graph/timezone, never
  a live DB read). No pytest test or CI workflow consumed them; only the
  manual `scripts/verify_m9_*.py`. The real gap was that the deterministic
  prompt stages had ZERO pytest coverage. Landed the pytest golden surface:
  `test_briefing_prompt_golden.py` (briefing — pin reproduces byte-identical
  under mocked Danny row, neutral fresh-tenant skeleton, per-owner no-bleed,
  fail-closed, determinism, per-tenant tz helpers), `test_classify_prompt_golden.py`
  (ingest — ROLE_UPDATE pin line reproduces under mocked graph, fresh-tenant
  neutral, fail-closed, per-owner cache isolation), `test_planner_prompt_golden.py`
  (decision — planner pin reproduces, non-IST tenant embeds JST) + a
  `tests/golden/README.md` documenting the three artifact classes. **The
  re-base caught a stale pin**: `planner_tenant1.txt` drifted 2 lines from
  the current prompt (the day-only-task routing change — "set
  params.deadline + null reminder_at") — regenerated from the current
  render after confirming the drift was the INTENDED behavior. briefing +
  classify pins reproduced clean (no change). §10 corrected accordingly.
- v2.7: Phase-3 #6 (boundary-clock matrix) executed. **38 new tests** +
  extraction of two embedded branches into pure functions: (1) the pulse
  schedule-window gate `briefing_due_now` — previously only a manual script
  (`scripts/verify_m9_7_schedule.py`), now a pytest matrix
  (`tests/unit/test_briefing_schedule.py`, pulse): window edges
  (07:44/07:45/08:15/08:16), weekday-vs-weekend slot separation,
  single-fire-per-heartbeat guarantee, midnight rollover, malformed-slot
  fail-closed, window clamp 1..15, `resolve_briefing_schedule`
  fail-closed→balanced, presets/picker sync; (2) the briefing-mode branch
  embedded in `_process_pulse_impl` extracted as pure
  `_resolve_time_intelligence()` (core/pulse/briefing.py) — the
  weekday/weekend/pre-Monday/Monday-morning/time-of-day matrix
  (`tests/unit/test_briefing_mode_matrix.py`, briefing): Monday-morning
  boundary (10:59 vs 11:00), afternoon window (12:00–15:29 incl. the
  15:00/15:30 flip), Friday wrap-up, night wind-down, Friday ≥19:00 weekend
  entry, Sunday ≥19:00 pre-Monday precedence, and the documented midnight
  edge (hour < 12 includes 00:00 → "Morning check."); the mode→pulse_mode
  mapping likewise extracted as `_map_pulse_mode()`. Also: sentinel
  calendar time-window tests (`tests/unit/test_sentinel_time_windows.py`,
  sentinel): post-meeting 5–30 min end-window filtering (boundary-inclusive,
  no-end-time skipped, wider fetch window) + upcoming-event bounds, frozen
  clock + mocked service; and **deferred-ledger X7 closed** —
  `get_upcoming_calendar_events` (frozen-clock 14-day bounds) +
  `delete_calendar_instance` now covered in test_google_orchestration.py.
- v2.6: Phase-3 #4 + #5 (calendar/email/sync orchestration + push) executed.
  **D1 RESOLVED → mock-orchestration-only**: `GOOGLE_REFRESH_TOKEN` drives
  real Google calls from cron workers (pulse/briefing/sentinel fan-out) and
  no test Google account exists, so the orchestration contract is proven
  hermetic against mocked services, with the real-API contract gated behind
  the existing opt-in `google_live` marker. Landed **42 hermetic tests** in
  four suites: `unit/test_google_orchestration.py` (calendar: event-body
  construction — priority prefixes 🔥/☕/⚡, description, popup reminders,
  end=start+duration, timezone; insert-vs-patch routing; recurrence;
  **404 heal-and-reprovision** (null DB id then re-insert); non-404 error
  propagation; no-creds skip; conflict check; day-scoped reads),
  `unit/test_tasks_sync_orchestration.py` (sync: completion patch on
  done/cancelled, insert-vs-patch, date-only vs datetime due, the 🕒
  explicit-time IST title hack), `unit/test_push_orchestration.py` (sync:
  byte-aware FCM payload truncation — emoji/CJK never split mid-sequence,
  empty/exact-fit passthrough; token dedup; android-high / ios apns config;
  data payload stringification; 404 invalid-token cleanup owner-scoped;
  silent data-only push), `unit/test_email_send_orchestration.py` (email:
  Gmail threading headers In-Reply-To/References + threadId fallback,
  reply-all CC collection excluding sender+self, status→'sent' BEFORE the
  API call (double-send guard), send-failure keeps 'sent', legacy
  Subject-line strip; Outlook 202 / 401 refresh-and-retry / no-token
  fail-clean). **Found + fixed a real latent bug**: `get_google_calendar_events`
  crashed on a plain `date` (`.replace(hour=...)` TypeError → silently
  returned [] → dropped calendar context); now accepts both datetime and
  date (whole-day scope).
- v2.5: Phase 3 #3 (learning-loop design + tests) executed. Design (D4
  resolved): the loop END is a two-phase contract — persist a decision
  (emit_observation → subsystem_patterns) then re-run the pipeline
  (compute_pattern_confidence) and assert behavior CHANGES (review →
  approve at MIN_PATTERN_OBSERVATIONS=3; demotion past MAX_ERROR_RATE=0.5).
  Landed: `tests/unit/test_learning_loop.py` (8 hermetic: escalation
  boundary, error-rate demotion, two-phase persist→re-run loop with a
  stateful fake client, undo-trains, fail-open) + `tests/sim/
  test_learning_loop_live.py` (2 live: real-DB two-phase flip + undo
  demotion, TEST-tenant, zero residue). **Found + fixed a vision-#4
  trust-breaker**: the undo paths (handler.py undo_auto_*, /api/
  auto-decisions/undo, /api/decisions/undo) reversed the decision and
  re-pended the item but never emitted a learning signal — the pattern that
  caused the wrong auto-approve stayed strong, so the same class of item
  kept getting auto-approved ("Not now that silently resets"). Fix: persist
  `metadata.learn_features` + `learn_subsystem` on the decision at record
  time (utils.py, email.py) and re-emit the INVERSE observation
  (`emit_undo_correction` — approval-undo → corrected, rejection-undo →
  confirmed) from every undo path. Leak guard extended to `decisions`
  (§5 table rule).
- v2.4: Phase 3 #1 (migration replay) executed. `scripts/replay_migrations.py`
  (scratch-cluster full-chain replay, numeric order, scaffold+base via
  `scripts/generate_replay_base.py` from backups/, structural-vs-data failure
  classification) + `tests/test_migrations_replay.py` (ops surface).
  Findings: the chain was NOT self-contained (7 pre-chain tables + core
  tables never created in-chain — base now generated from backups/);
  lexicographic sorting would run db/100 before db/10 (numeric order
  enforced); **db/101 was broken for fresh replays** — it dropped
  retrieval_triples while db/04's retrieval_edges still FK'd to it (prod
  masked because the FK never existed there) — fixed inline (IF EXISTS
  no-op on prod). 94/94 migrations now replay clean + 1 expected data skip
  (db/06 references pre-chain rows).
- v2.3: Phase 2 (UAT absorb) executed. Webhook `TEST_CHAT_IDS` allow-list
  (default-off fail-closed; negative-test matrix in test_webhook_auth.py),
  harness fixes (mock signature `notify_push`/`intent`/`ack_title` — the
  08-13 killer; dedicated TEST_CHAT_ID 909999999 — no owner impersonation;
  owner-scoped `_delete_ilike`/cleanup), thin pytest adapter
  `tests/uat/test_uat_l4.py` (22 scenarios, per-scenario aspects,
  session tenant-scope + patchers + cleanup), leak guard extended to
  `[UAT]%` + UAT chat id, `run_tests.py e2e` tier, harnesses archived
  (run_full_uat.py + diag_s5.py → scripts/archive/README-uat.md). Verified
  live: all 22 scenarios pass against the TEST tenant, zero residue. UAT
  pacing cut 4s→0.5s (Gemini limiter self-paces via acquire_async).
- v2.2: Phase 0 (inventory) + Phase 1 (foundation, partial) executed.
  Inventory → `docs/test-inventory.md`. Landed: runner (`run_tests.py`),
  Makefile, pre-push hook, CI split (fast on push / nightly scheduled),
  `pytest-cov` + `freezegun` deps, 13 aspect markers + `llm_live` +
  `google_live` (strict-markers; `integration` marker dropped), all 78 test
  modules tagged (module-level `pytestmark`), marker-presence lint (L0),
  `frozen_clock` fixture, `tests/README.md` contract, aspect selection
  (`run_tests.py <aspect>` works). Also surfaced + fixed: CI residue gate red
  since Aug 10 (false positive `entity_detector.py` 'ministry' → scoped
  ALLOW_PAIRS exemption). REMAINING Phase 1: L2-mock build (~113 live-only
  tests), per-layer cov floors, teardown batching (nightly ceiling).
- v2.1: fast/nightly split hardened into an INVARIANT — **fast contains no
  live-DB tests, period**. L2 splits into L2-mock (hermetic, qualifies for
  fast) and L2-live (real rows/schema, nightly by default). Decision rule and
  pacing-sleep rule stated in §2 so Phase 1 never makes the call mid-flight.
  Phase 0's question narrows from "does fast fit?" to "does nightly fit?".
- v2.0: solo-dev operating reality · Phase 0 inventory · map-don't-duplicate
  existing CI (21 workflows, `-c /dev/null` finding) · third harness
  (`diag_s5.py`) · leak-guard table growth rule · open-decisions ledger
  (D1–D5) · cassettes cut · dashboard parked.

The definitive blueprint for the Rhodey OS test suite rebuild. Everything the
suite must cover, how it is organized, how any slice can be run, and the
rules that keep it honest. This document supersedes ad-hoc test additions.

Status: **IN PROGRESS** — Phases 0–2 + Phase-3 #1–#6 + **Phase 4** done.
Foundation + UAT absorb + migration replay (caught a real db/101 chain bug)
+ learning-loop design (fixed a real vision-#4 undo-training trust-breaker)
+ calendar/email/sync orchestration + push (42 hermetic tests; D1 closed;
fixed a latent `get_google_calendar_events` date-crash) + boundary-clock
matrix (38 tests; X7 closed) + golden re-base (stale planner pin; §10
corrected) + API contract (pinned surface; fixed 8 duplicate operationIds)
+ health-as-L3 mapping + security negatives + Flutter goldens + **CI
rewritten around run_tests.py** (`-c /dev/null` killed; Flutter in the push
gate; nightly wired).
Remaining: dashboard (parked, D3) + flaky-quarantine mechanism (§15,
policy-on-demand — no flaky tests to quarantine yet).

Phase-1 leftovers **all closed** (v2.14): teardown batching (X6) ✅ and
per-layer cov floors ✅ (core/COV_FLOOR + api/API_COV_FLOOR from one
measurement).

Deferred ledger **X2–X5 all closed** (v2.15); **X1 decided: leave the
`projects` table as-is** (live audit: 37 real dormant rows, zero writers,
load-bearing as the test harnesses' live parent — see §19).

> **Operating reality (this plan is written for a solo developer).**
> There is no UAT team and no QA team. The author of this plan is also its
> only tester, reviewer, and on-call. That shapes every design decision:
> gates must fit in CI time budgets a solo dev can maintain, flaky policy
> must be self-enforced (no reviewer to police a "1 merge-cycle" rule), UAT
> scenarios are regression safety for one person, not a handoff artifact,
> and any automation that takes more time to maintain than the feature it
> protects is debt. Judgment over volume — everywhere in this document.

---

## 1. Goal & Non-Negotiable Principles

The suite exists to answer one question on every feature release and bug fix:
**"Did this change break anything, anywhere?"** — and to answer it for any
single aspect in isolation.

1. **Every live test runs against the Test tenant — never a real tenant.**
   The Test user is the only writable surface for anything that touches the
   DB. No fallback to the channel tenant. No test that can't resolve the Test
   tenant runs live — it skips or fails, it never leaks.
2. **Run any aspect, run everything.** Aspect markers give single-command
   selection; the runner gives one entry point for all layers and surfaces.
3. **Deterministic or it doesn't exist.** Clock and LLM are the two flakiness
   sources; both have explicit policies (§8). A test whose result changes when
   the model drifts is a determinism bug.
4. **Coverage is proven by lint, not vibes.** Marker-presence lint + one
   per-layer floor; a feature can't land with zero tests on its aspect.
5. **The suite is a gate, not a museum.** It runs in CI on every release;
   dead/archived tests leave the tree (archive dir), and everything that
   stays is runnable today. For a solo dev, a suite that takes >5 min to run
   the fast tier is one that stops getting run.
6. **Start from the present.** The plan's target state is built on a map of
   what already runs today (Phase 0) — existing CI, existing workflows,
   existing harnesses — not on a blank page.

---

## 2. The Test Pyramid

```
L0  STATIC GATES      ruff · residue scan · marker-presence lint          (s, no DB)
L1  UNIT              per-module, mocked DB/LLM/clock                     (min, no DB)
L2-mock  FLOW         sim/cluster tests with hermetic fakes, no DB       (min, no DB)
L2-live  FLOW         sim/cluster real-DB variants (what they are today) (min, Test tenant)
L3  INTEGRATION       tenants · API · golden · sync · health · migrations (min-hr, Test tenant)
L4  E2E/UAT           absorbed UAT scenarios, test-tenant scoped          (long, Test tenant)
```

Speed split (runner §12): **fast** = L0 + L1 + L2-mock + app ·
**nightly** = L2-live + L3 + L4 + coverage + leak guard.

### 2.1 The no-live-DB-in-fast INVARIANT (decided now, not in Phase 0/1)

**The fast tier contains no live-DB tests, period.** L2 splits into two
flavors:

- **L2-mock** — sim/cluster tests runnable with hermetic fakes (no DB).
  Qualifies for fast.
- **L2-live** — the real-DB variants (what sim/clusters do today: real rows,
  real schema). Nightly by default.

Decision rule (applies the moment a test is written or moved):
- A live test whose core assertion survives mocking → add the L2-mock
  variant, keep the live one nightly.
- A live test whose assertion needs the real DB → nightly, no mock variant
  forced.
- **Any test with pacing sleeps (rate-limiter avoidance, UAT-style) → nightly
  automatically.** The UAT harness's `CLASSIFY_PACING_S` and the 15/60s
  rate-limiter pacing are exactly the kind of thing that would detonate a
  5-min budget, and they are live-only anyway.

Budget discipline: **fast must fit inside 5 minutes** (a solo dev runs it on
every change, even uncommitted) and **nightly must fit inside 20 minutes** —
that is the ceiling already imposed by the existing CI job's
`timeout-minutes: 20` (see §14). If a tier blows its budget, the response is
to move tests down a tier or shrink scope, not to raise the ceiling — but
now the "never raise the ceiling" rule bites inside **L2-mock** (convert to
L1-style hermetic), which is cheap, instead of inside live-DB, which is not.
The runner's first run in Phase 1 measures actual durations and records them
here as the baseline.

Phase 0 still measures L2's live share — not to decide fast's composition
(settled above), but to verify the 20-min nightly ceiling holds once
L2-live + L3 + L4 share it, and to flag any suite that needs to shed live
variants.

---

## 3. Aspect Taxonomy (13, exclusive-primary)

Each test carries **exactly one primary aspect** (this is what `-m` selects)
plus free secondary tags. Coverage is counted per-primary-aspect only.

| Aspect | Product surface (Layer 1–6 ref) | Primary suites today |
|---|---|---|
| `pulse` | Pulse engine, run gating, schedule, heartbeat | unit/briefing_refresh, briefing_schedule (window matrix), clusters/timing_scheduling |
| `briefing` | Briefing generation, sections, golden output | golden/briefing_tenant1, unit/health_fixes, briefing_mode_matrix |
| `sentinel` | Nudges, sweeps, zombie recovery, vps capture | unit/sentinel_provenance, sentinel_time_windows |
| `decision` | Approve/reject/undo, auto-decisions, ledger | unit/decision_undo, unit/awaiting_reply |
| `learning` | Decision → persist → subsequent behavior change (vision #4 loop-end) | unit/learning_loop (escalation, demotion, two-phase), unit/telemetry T1-T7, learning_hints, pattern_extractor, sim/learning_loop_live |
| `ingest` | Telegram/WhatsApp/Teams/Email/Call/Beeper channels, classifier, DLQ | unit/teams_ingest, whatsapp_golden, beeper_*, sim/suite* |
| `webhook` | Webhook auth, callback routing, test-chat bypass | **thin** — root/test_webhook_utils (7 lines) |
| `auth` | OTP, API keys, tenant provisioning, RLS, settings fallback | unit/auth_provision, tenants/*, unit/user_settings |
| `calendar` | Google/Outlook sync, events, conflicts | unit/google_orchestration, clusters/deletion_cancellation, timing_scheduling |
| `email` | Gmail/Outlook ingest, classify, send | unit/email_*, email_send_orchestration, sim |
| `sync` | Push notifications, calendar/task two-way sync orchestration | unit/push_orchestration, tasks_sync_orchestration |
| `retrieval` | Indexing queue, hybrid search, tsvector, eval | unit/retrieval-ish + test_retrieval (519 lines), tsvector |
| `graph` | Nodes/edges, backfill, merges, lineage, memory clusters | unit/graph_pipeline, backfill_graph, clusters/* |

Tagging decisions (explicit, decided here so Phase 1 is mechanical):

- **Settings/config fallback** (`test_settings_fallback`, `user_settings`,
  `core_config`) tags **`auth`** — it is the per-tenant permissions/settings
  surface. Not its own aspect; nobody types `-m settings`.
- **Ops surfaces** (rate limiter, providers/failover) carry no primary aspect;
  they are covered by per-layer floors and tag with the layer only.
- **`app` (Flutter)** and **`dashboard` (frontend/)** are NOT pytest aspects —
  they are separate runtimes, orchestrated by the Makefile, never `-m`.
  See §6 and §14 for their status (dashboard is parked).

---

## 4. Surface × Coverage Matrix (the study)

Current state measured against the canonical architecture (Layer 1–6).
Legend: ✅ covered · ◐ partial · ❌ gap · ⏸ parked (deliberate, §15).

| Product surface | Aspect | Current | Where today | Missing |
|---|---|---|---|---|
| Telegram ingest | ingest | ◐ | sim/suite*, webhook | dedicated channel tests thin |
| WhatsApp ingest | ingest | ✅ | whatsapp_golden, sim | — |
| Teams ingest | ingest | ✅ | teams_ingest | — |
| Email ingest | email | ◐ | email_classify_prompt, email_learning | pipeline e2e, Outlook ingest path |
| Email draft send (Gmail/Outlook) | email | ✅ | email_send_orchestration (threading, CC, double-send guard, 401 refresh) + email_learning | — |
| Call ingest | ingest | ❌ | — | none |
| Beeper bridge | ingest | ✅ | beeper_desktop/ingest/send | — |
| Document extraction | ingest | ❌ | — | none |
| DLQ consumer | ingest | ❌ | — | none |
| URL quarantine | ingest | ✅ | url_shortcut | — |
| Classifier | ingest | ◐ | classify_project_update, dispatch_heuristics, classify_prompt_golden (ROLE_UPDATE pin) | full matrix |
| Action planner/executor | decision | ✅ | action_models, actions, executor_acks, executor_patch, batch_concurrency, planner_prompt_golden | — |
| Entity resolution | graph | ✅ | entity_hardening, insert_extracted_entities, mentions_provenance | — |
| Enrichment queue | retrieval | ✅ | chunk_enrichment, eval_harness, index_queue(sim) | — |
| Context registry | retrieval | ✅ | context_registry (unit+sim), preflight_context | — |
| Threads/workflows | ingest | ◐ | thread_classification, sim | conversation_workflows lifecycle |
| Brain synthesis | retrieval | ❌ | — | none |
| Pulse gating/schedule | pulse | ✅ | briefing_schedule (window edges, single-fire, weekday/weekend, midnight) + timing_scheduling | — |
| Briefing sections | briefing | ✅ | health_fixes, briefing_prompt_golden (pin + neutral skeleton + tz helpers), briefing_mode_matrix | — |
| Sentinel nudges | sentinel | ◐ | sentinel_provenance, sentinel_time_windows (end/upcoming windows) | app-only push path |
| Zombie recovery | sentinel | ✅ | health_fixes | — |
| Decision undo/ledger | decision | ✅ | decision_undo, health_fixes | — |
| **Learning loop end** | learning | ✅ | unit/learning_loop (8), sim/learning_loop_live (2), telemetry T1-T7, learning_hints, pattern_extractor | — (undo-training trust-breaker fixed in v2.5) |
| Push notifications | sync | ✅ | push_orchestration (truncation, dedup, platform config, 404 cleanup, silent) | — |
| Calendar sync | calendar | ✅ | google_orchestration (body, patch/insert, 404 heal) + deletion_cancellation | real-API contract = opt-in `google_live` (D1 closed: mock-orchestration-only) |
| Google Tasks two-way | calendar | ✅ | tasks_sync_orchestration (completion, insert/patch, 🕒 hack) | — |
| Auth OTP/API key | auth | ✅ | auth_provision + auth_negatives (attempt cap, rate limit, anti-enumeration, fail-closed 503, wrong-key 401, disabled user) | — |
| Webhook auth gate | webhook | ✅ | webhook_auth (bypass allow-list default-off + negatives) | — |
| Tenant isolation | auth | ✅ | tenants/*, conftest leak guard | — |
| Settings fallback | auth | ✅ | user_settings, settings_fallback | — |
| Retrieval/search | retrieval | ✅ | test_retrieval, tsvector, match_* | — |
| Graph backfill/merge | graph | ✅ | backfill_graph, graph_pipeline, clusters/merge_dedup | — |
| Memory clusters | graph | ◐ | clusters | — |
| Rate limiter/breaker | (ops) | ✅ | rate_limiter, providers_shape | — |
| LLM providers/failover | (ops) | ✅ | providers_shape | — |
| Migrations replay | (ops) | ✅ | test_migrations_replay (94/94 clean; caught db/101 bug) | — |
| Health/validate gates | (ops) | ✅ | covered-by-workflow: health.yml → run_health.py (scheduled+dispatch+alert) + validate_deployment.yml (post-CI) + health_wrapper (business-hours, CLI contract, fan-out isolation) | — (mapped, not duplicated — §14.2) |
| API contract (BFF) | (ops) | ✅ | api_contract (pinned 78-path/89-op surface + spec validity) | — (route changes update the pin, reviewed like code) |
| App (Flutter) | app | ● | rhodey_app/ 12 files / 62 tests incl. 2 goldens (briefing card + task ack) + 2 integration_test on a booted emulator | integration_test ✅ (X8 closed v2.13 — Pixel 8a AVD) |
| Dashboard (Next.js) | dashboard | ⏸ | frontend/ — lint-only in CI | **parked** — personal tool, refactor-to-admin-panel decision pending (§16 D3) |

---

## 5. Aspect Markers — Semantics & Lint

- **Exclusive-primary**: one primary aspect per test; `pytest -m pulse` selects
  it; `-m "pulse and briefing"` intersects; secondary tags free.
- **Marker lint** (L0): a script that fails CI if any new/modified test file
  (or test function) lacks an aspect marker — the anti-0% rule. "Modified" is
  detected by git diff against the merge-base of the push/PR target branch.
- Registered in `pytest.ini` (`markers =`); unknown markers are errors
  (`--strict-markers`).
- **Remove the stale `integration` marker** (Phase 1): it is registered in
  pytest.ini but used by zero tests. With `--strict-markers` it is legal but
  `-m integration` silently no-ops. Drop it from the registry; layer is
  expressed by directory + runner mapping, not by an `integration` marker.
- `llm_live` marker: tests that call the real LLM — excluded from default
  `nightly` runs, opt-in only (cost guard, §8.2).
- **Leak-guard table growth rule**: every Phase-3 gap closure that touches a
  new table MUST extend `_LEAK_MARKER_TABLES` (and the thread/chat-id
  patterns) in `tests/conftest.py` in the same change. The guard only knows
  the tables it lists; new surfaces can leak invisibly otherwise.

---

## 6. Runners Outside Pytest

| Surface | Runtime | Status | Orchestrated by |
|---|---|---|---|
| `app` (Flutter) | `flutter test` in `rhodey_app/` (hermetic) + `flutter test integration_test` on a booted emulator (on-device) | 62 tests (58 widget + 2 goldens + 2 onboarding nav) hermetic; 2 integration_test on-device | `make test-app` · `make test-app-goldens` (regenerate) · `make test-app-integration` (emulator required) |
| `dashboard` (Next.js, `frontend/`) | vitest | **parked** — lint-only in CI today; logic tests deferred until the admin-panel refactor decision (§16 D3) | `make test-dashboard` (stub) |

These are never `pytest -m` targets. The runner's `fast` tier invokes
`flutter test`; `make test-dashboard` stays a no-op stub that prints "parked"
until D3 resolves.

---

## 7. Test Tenant & Sandbox Contract

- **Test tenant only**: `resolve_test_tenant_uid()` (tests/fixtures/
  test_tenant.py). Never the channel tenant. Live layers skip when unresolvable.
- **Env contract** (documented in tests/README.md):
  `LIVE_DB=true` · `TEST_TENANT_UID` · `TEST_TENANT_NAME` (default "Test") ·
  a whitelist of legal `SUPABASE_URL` refs — the runner **refuses** to run
  live layers against an un-whitelisted project (never silently hit prod).
  The whitelist is committed in the runner and lists only the known project
  ref(s) (the shared project today — see §16 D2).
- **Clean-slate**: session fixture deletes test-tenant marker rows **before**
  the suite and the existing post-session leak guard stays (fail-closed).
  Interrupted runs poison the *next* run's leak guard; the pre-delete makes
  the next run self-healing.
- **Per-run chat allocation**: replace baked `_TEST_CHAT_IDS` /
  `sim 999999999` / `9000000+offset` with per-session unique chat_ids, seeded
  into `users.telegram_chat_id` for the Test user → kills the CI↔local race
  on the shared sandbox (Test doubles as the manual phone). The fixed
  `_TEST_CHAT_IDS` set in conftest becomes the *legacy* pattern the leak guard
  still knows, while new runs use session-scoped ids.
- **Sequential-only** for L2–L4: no pytest-xdist for live layers. The shared
  tenant is a single writable surface; parallelism races it. (pytest-xdist is
  fine for L1, which never touches the DB.)
- **Webhook bypass contract** (production auth change — the ONLY sanctioned
  test-motivated change to the auth boundary):
  - Today `core/webhook/handler.py` accepts a single chat id (env
    `TELEGRAM_CHAT_ID`); the UAT harness currently gets past it by
    *impersonating* a chat (hardcoded `"first_name": "Danny"`). That
    impersonation must die.
  - Replace with an explicit allow-list: `TEST_CHAT_IDS` env, consulted
    **only when set**. Default-off — no env → no bypass → behavior identical
    to today. Fail-closed.
  - The negative test is the guard: any chat outside the allow-list is
    rejected, and the bypass is asserted OFF when `TEST_CHAT_IDS` is unset
    (webhook aspect, Phase 2).
- **Baseline drift rule**: live assertions never depend on pre-existing
  Test-tenant state; `SIM_TEST` demo rows are tolerated and excluded.
- **The harness cannot force owner_id** — rows created through the app's own
  code paths are owned per the app's resolution from the bound chat
  (`resolve_telegram_chat_id`, core/services/db.py). Tenant scoping of UAT
  therefore means *sending from a chat bound to the Test user*, not wrapping
  inserts. This is the real engineering of Phase 2 (see §13).

---

## 8. Determinism Policy

### 8.1 Clock — `frozen_clock` fixture

- One shared fixture: `frozen_clock(iso_ist)`, anchored to `Asia/Kolkata`
  (repo standard: timezone-aware, never fixed offsets).
- Pulse/sentinel/briefing/scheduling tests **must** freeze the clock; the
  runner flags time-dependent aspects if unfrozen.
- Boundary matrix: weekday/weekend, pre-Monday, Monday-morning, midnight
  edge, month edge — the briefing-mode branches (§4 pulse/schedule gap).
- Dependency: `freezegun` (add to requirements).

### 8.2 LLM — two sanctioned mechanisms

(a) **Hermetic fake** of `generate_content_with_fallback` — the default for
L1/L2. Deterministic, instant, no network.

(c) **Golden/pinned output** compare — briefing/classify/planner already have
golden artifacts; re-based to the Test tenant (§10). The L3 mechanism.

> ~~(b) Recorded cassettes~~ — **cut in v2.0.** No VCR/cassette infrastructure
> exists in the repo, and goldens already pin responses. Adding VCR.py + a
> recording workflow solves a problem (c) already solves. Reintroduce only if
> a concrete multi-call recording need appears and is justified.

- Real-LLM tests: `llm_live` marker, opt-in, budget-capped, never in default
  `nightly`.
- Rule: a test that would change result when the model drifts is a
  determinism bug — must move to (a) or (c).

---

## 9. Migration & Schema Contract

- **Migration-replay test (L3, high priority — cheap, catches real
  breakage):** apply all committed migrations against a scratch schema in
  order, assert none fail and the final schema matches the known-good shape.
  Today `scripts/apply_migrations.py` exists but no test replays it.
- **Precondition discipline:** live layers run against the schema as it
  exists in the shared project. The runner's `nightly` does NOT auto-apply
  migrations to the shared project — that is an explicit, human-invoked
  step (separation between "test the migration" and "apply the migration").
- **Destructive-migration awareness:** the repo has drop-migrations
  (e.g. db/75_drop_entity_mirror_tables.sql). Migration-replay tests them in
  a scratch schema; never replay history against the live project.
- The production-vs-test-project question is stated, not inherited — §16 D2.

---

## 10. Golden Policy

Three artifact classes (see `tests/golden/README.md`), one rule: **no pytest
 golden depends on a real tenant's live data — every golden is hermetic
 (mocked fixtures, never a live DB read).**

- **Channel-tenant regression pins** (`briefing_tenant1.txt`,
  `classify_tenant1.txt`, `planner_tenant1.txt`): tenant #1's pinned OUTPUT
  shape. The `_tenant1` name is accurate AND intentional — these pin Danny's
  exact output so a prompt refactor that changes it fails the gate. They are
  hermetic (the row/graph/timezone used to reproduce them is mocked) and do
  NOT violate the Test-tenant principle (§7). Consumed by the pytest golden
  surface below + the manual `scripts/verify_m9_*.py`.
- **Hand-labeled input corpus** (`whatsapp_classify/golden.json`): real chat
  threads as INPUT fixtures with hand labels for the deterministic sieve/ask
  stages — not tenant output, no re-base applies.
- **Pytest golden surface** (`test_briefing_prompt_golden` (briefing),
  `test_classify_prompt_golden` (ingest), `test_planner_prompt_golden`
  (decision)): byte-identical pin reproduction + neutral fresh-tenant
  behavior + fail-closed + determinism + no cross-tenant bleed.
  Aspect-tagged, runnable via runner (`run_tests.py <aspect>`).
- Golden diffs are reviewed like code; a prompt/parser change requires an
  intentional golden update (this is what would have caught the
  briefing-card formatting bug class). A stale pin regenerates from the
  current render with the SAME fixtures the test uses — reviewed and
  committed, never a silent overwrite.
- History: v2.8 re-based `planner_tenant1.txt` (2 intentional lines — the
  day-only-task deadline routing change); briefing + classify pins
  reproduced clean, no change needed.

---

## 11. Coverage Policy

- **Marker-presence lint** (§5) is the anti-zero rule.
- **Per-layer floors, one measurement** (v2.14): the nightly pytest-cov run
  measures once; `coverage report --include` then enforces a floor per source
  layer from that same data — `core/*` under `COV_FLOOR`, `api/*` under
  `API_COV_FLOOR` (defaults 20, env-configurable, fail-closed on 0%). No
  extra pytest runs, so no 13× `--cov` runtime. Reported per aspect but not
  gating per-aspect (avoids gamed thresholds).
- Coverage report is an artifact of `nightly`, not a push gate.
- A solo dev's coverage is only worth what the fast tier runs; coverage
  numbers for layers not running in CI are decoration.

---

## 12. Runner & Commands

`scripts/run_tests.py` + Makefile. Single entry point; owns env resolution,
aspect→path mapping, whitelist refusal, and budget enforcement.

```bash
python scripts/run_tests.py all            # full gate (fast + nightly, live layers)
python scripts/run_tests.py fast           # L0+L1+L2-mock + app  [must fit 5 min]
python scripts/run_tests.py nightly        # L2-live+L3+L4 + coverage + leak guard  [must fit 20 min]
python scripts/run_tests.py pulse          # aspect, any layer
python scripts/run_tests.py --layer unit   # tier
python scripts/run_tests.py --coverage     # per-aspect report
python scripts/run_tests.py --regen-goldens
python scripts/run_tests.py --inventory    # Phase-0 report: current state vs §4 matrix
```

```bash
make test            # fast
make test-nightly
make test-pulse      # one aspect (maps to run_tests.py)
make test-app        # flutter test in rhodey_app/
make test-dashboard  # STUB → prints "parked" until D3 resolves
```

Runner contract:
- Resolves and validates the env contract (§7); refuses un-whitelisted
  `SUPABASE_URL`.
- `--coverage` runs once per layer (not per aspect) and aggregates per-aspect
  numbers from the single run.
- Reports the fast/nightly wall-clock budget status each run; a tier over
  budget prints the movers to shrink.

---

## 13. UAT Absorption (Phase 2) — wrap, don't rewrite

**Harness inventory (from Phase 0 — there are THREE, not two):**

| Harness | Lines | Scenarios | Nature | Fate |
|---|---|---|---|---|
| `tests/uat/run_uat.py` | 1,697 | 22 | mocked sends, async scenarios, `simulate_telegram` impersonating a chat | **Absorb** as L4 |
| `tests/uat/diag_s5.py` | 855 | 17 | predecessor harness, own `_mock_send_telegram` | **Overlap-check → archive** |
| `scripts/run_full_uat.py` | 2,158 | multi-layer | interactive `input()` HITL (waits for real Telegram Decision Pulse approval), stale 08-04, has `SKIP_HITL` | **Archive from gate; keep as documented manual tool** |

**Absorption mechanics:**
- **Absorb** `run_uat.py`'s 22 scenarios as L4: one parametrized
  `test_scenario_N` adapter per scenario, calling the existing async body
  unchanged. Selection/reporting/leak-guard come free from pytest.
- **Two surgical fixes before absorption** (confirmed against code):
  1. `_mock_send_telegram` gains the missing kwargs the real
     `send_telegram` accepts (`notify_push`, `intent`, `ack_title` —
     core/webhook/telegram.py:27). Today the uat main fails with a TypeError
     exactly because the mock lacks `notify_push`.
  2. `_delete_ilike` cleanup becomes owner-scoped
     (`.eq('owner_id', TEST_TENANT_UID)` + marker prefix) — today it is a
     pattern-only delete, the exact leak shape P1 exists to prevent.
- **Tenant scoping is real engineering** (§7 per-run chat): simulated
  messages must originate from a chat bound to the Test user so the app's
  own resolution assigns ownership — the harness cannot force owner_id.
- **diag_s5 fate:** diff its 17 scenarios against the 22 in run_uat.py. If
  (expected) they overlap, archive it after confirming every scenario it
  covers is covered by a passing absorbed test; if any scenario is unique,
  port that one scenario's body via the same adapter.
- **Solo-dev note:** `run_full_uat.py` as a *manual* tool has real value for
  a one-person shop — it exercises the live Decision Pulse approval loop no
  automation can. Archive it out of the pytest tree (so the gate stays
  clean) but keep it runnable on demand with a README stating it writes to
  the Test tenant only and requires human interaction.

---

## 14. Existing CI & Workflows — Map, Don't Duplicate

**Phase 0 finding: the plan's target state is not greenfield. Today the repo
already has 21 GitHub workflows, including a full-suite live-DB CI job.**

### 14.1 `test.yml` today (the gate that already exists)

`.github/workflows/test.yml` on push/PR to main:
- `ubuntu-latest`, `timeout-minutes: 20`.
- Secrets wired: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY
  (+_2, _3), OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  PULSE_SECRET, GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID/SECRET,
  UPSTASH_REDIS_REST_URL/TOKEN.
- Steps: non-fatal secret check → py3.11 → pip install → `ruff check .` →
  **frontend lint** (`cd frontend && npm ci && npm run lint`) → **residue
  gate** (M17, `scripts/scan_tenant1_residue.py`) → unit tests.
- Test step: if `LIVE_DB=true`, `pytest tests/ -v -c /dev/null | tail -50`
  (the FULL suite, live DB, every layer, one job); else `pytest tests/unit/`.

**Implications the plan must respect:**
1. **The live gate already runs on every push.** The fast/nightly split
   REPLACES an all-in-one job; it does not add CI where none existed.
2. **`-c /dev/null` discards pytest.ini entirely** — today's CI respects no
   markers, no testpaths, no skip logic. The runner must become the CI entry
   point to fix this.
3. **The 20-minute timeout is the nightly ceiling** (§2), already enforced.
4. **Google OAuth creds are already CI secrets.** Before assuming
   "calendar = mocks only", establish what `GOOGLE_REFRESH_TOKEN` actually
   drives in CI today (find the consuming code path in Phase 0). §16 D1.
5. **Frontend lint is already a gate** — keep it; dashboard test scope is
   the only thing parked (§16 D3).
6. **The M17 residue gate already exists** — it is the "L0 residue scan" in
   the pyramid. Do not rebuild it; wrap/retain it.

### 14.2 Workflow inventory (21) and disposition

Existing: backfill_graph, call_ingest, clean_duplicate_nodes, concept_sweep,
decision-pulse, dedupe_pending, email_ingest, flutter-distribute, health,
ingest, memory_clusters, notebooklm-sync, persona_synthesis, pulse,
renew_drive_channel, research_worker, retrieval_backfill, sentinel,
synthesis, test, validate_deployment.

Disposition:
- **`test`** — the gate; evolved per §15.
- **`health`, `validate_deployment`** — these ARE the "health/validate as L3"
  items in the matrix. Map them: either the runner invokes them, or they stay
  as workflow gates and the matrix row records them as covered-by-workflow.
  Do NOT build a parallel test representation.
- **Cron/event workers** (ingest, pulse, sentinel, email_ingest,
  call_ingest, backfill_graph, retrieval_backfill, memory_clusters,
  concept_sweep, clean_duplicate_nodes, dedupe_pending, research_worker,
  persona_synthesis, synthesis, notebooklm-sync, renew_drive_channel,
  decision-pulse) — operational, not test gates. A Phase-0 inventory records
  what each runs so the test matrix references real entry points.

### 14.3 Residue scan

`scripts/scan_tenant1_residue.py` (M17) fails CI if any tenant personal
identifier appears in shared runtime code. This is L0 already done. The
marker-presence lint (new) is a separate L0 check; both run in fast.

---

## 15. CI Design (evolve test.yml)

- **Push/PR: fast gate** — L0 (ruff + residue + marker lint) + L1 +
  L2-mock + app (no live-DB tests, per the §2.1 invariant). Must fit the
  20-min job with headroom (budget: ≤12 min of test time).
  LIVE_DB=true only when secrets present (current behavior preserved), and
  only against whitelisted refs (§7).
- **Merge + nightly: nightly** — L3+L4 + coverage + leak guard + migration
  replay + health suite. Scheduled (cron) and `workflow_dispatch`, plus the
  existing push/PR trigger for `nightly` only when a `[nightly]` label or a
  workflow-edit path demands it (avoid burning the 20-min budget on every PR).
- **Solo-dev flaky policy (self-enforced, no reviewer):** flaky tests land
  in `tests/quarantine/` — skipped but reported. The rule is one hard wall:
  **a test that flakes twice in a month gets quarantined in the same week**;
  quarantine is not a shelf, it is a debt item with a date — the quarantine
  report lists what's parked and how long it has been there, surfaced in the
  runner's `--inventory` output. No merge-cycle bureaucracy; just the debt
  ledger, because there is nobody else to police it.
- **Secrets matrix per job:** live layers only where `TEST_TENANT_UID` is set
  and `SUPABASE_URL` is whitelisted.

---

## 16. Open Decisions (tracked, not silent)

| # | Decision | Default | Blocking |
|---|---|---|---|
| D1 | Calendar/email: real-API matrix vs mock-orchestration-only | **RESOLVED (v2.6): mock-orchestration-only.** Evidence: `GOOGLE_REFRESH_TOKEN` drives real Google calls from cron workers (pulse fan-out, briefing, sentinel) and there is no test Google account — the orchestration contract is proven hermetic (42 tests, v2.6) and the real-API contract stays an opt-in `google_live` gate if a sanctioned test Gmail/Calendar account ever appears. | ✅ closed |
| D2 | Is the shared Supabase project the production DB? (evidence: Danny's TELEGRAM_CHAT_ID + Google creds + Gemini keys as CI secrets) | Assume yes; document Test-tenant-in-prod + leak guard as the deliberate model; name a separate test project as the alternative and reject for cost/time — but the decision is stated, not inherited | Phase 1 (whitelist), Phase 3 (migrations) |
| D3 | Dashboard future: refactor to admin panel? | Parked (v2.0). Revisit when the refactor starts; logic tests only, no visual-design tests | Phase 3 dashboard items |
| D4 | Learning-loop test design | **RESOLVED (v2.5).** The loop END is a two-phase contract, and the tests prove it in both directions: (1) persist — a decision flows through emit_observation → subsystem_patterns rolling counter; (2) re-run — compute_pattern_confidence on the SAME features returns a DIFFERENT recommendation than before the decisions (review → approve at MIN_PATTERN_OBSERVATIONS=3; corrections past MAX_ERROR_RATE=0.5 demote back). Implemented: unit/learning_loop (escalation boundary, demotion, stateful two-phase loop, undo-trains, fail-open) + sim/learning_loop_live (real-DB two-phase flip + undo demotion). The design also surfaced + fixed a real vision-#4 trust-breaker: undo paths emitted NO learning signal, so a wrong auto-approve's pattern stayed strong. Fix in v2.5 (learn_features on decisions + emit_undo_correction from every undo path). | ✅ closed |
| D5 | `TEST_TENANT_UID` source of truth | CI sets it explicitly; local resolves the `Test` user row. Both paths already exist in `resolve_test_tenant_uid()` | Phase 1 |
| D6 | api-layer coverage gap — measured 2026-08-16: api/index.py 12%, api/briefing.py 10%, TOTAL api 12% (default floor 20 was never validated) | **ACTION (v2.17):** floor calibrated to `API_COV_FLOOR=10` (measured-baseline pattern, like core 23→20). Real fix: lift api ≥20 to restore the default — target `api/briefing.py` (the live briefing builder, 741 stmts) and the highest-value `api/index.py` handler paths. | Not blocking; tracked follow-up |

---

## 17. Phases & Delivery

| Phase | Contents | Exit criteria |
|---|---|---|
| **0 Inventory** | Map the 21 workflows; record test.yml's exact behavior; inventory the 3 UAT harnesses; confirm the baseline is green (run today's pytest baseline — mock mode — record pass/fail and wall-clock); measure L2's live-DB share to verify the 20-min nightly ceiling holds once L2-live + L3 + L4 share it, and flag suites needing to shed live variants (fast's composition is settled, §2.1); identify what `GOOGLE_REFRESH_TOKEN` drives in CI; confirm the D2 production-project question; count tests with a stated convention (~753 `def test_`/`async def test_` functions today) | An `--inventory` report that reconciles §4 against reality; baseline durations recorded (fast fit confirmed, nightly ceiling verified); D1/D2 evidence gathered |
| **1 Foundation** | Drop stale `integration` marker · register 13 markers + strict registration · tag existing tests (script-assisted, keyed off the §3 "Primary suites today" column) · marker-presence lint · per-layer cov floors · `run_tests.py` + Makefile · env-contract guard (whitelist) · `frozen_clock` fixture · freezegun dep · LLM determinism policy doc (§8.2) · leak-guard-table growth rule documented · L2-mock/L2-live split applied to sim/clusters per §2.1 decision rule | `run_tests.py <any-aspect>` works; lint passes; baseline suite green; fast tier (L0+L1+L2-mock+app) measured and fits 5-min budget; no live-DB test in fast |
| **2 UAT absorb** | Webhook test-chat bypass (default-off allow-list + negative test) · per-run chat seeding · owner-scoped cleanup · 22 scenarios as L4 via adapter · diag_s5 overlap-check → archive · run_full_uat.py archived to manual-tool with README · retire the manual harnesses from the gate | `run_tests.py e2e` green, test-tenant only, leak guard clean, no chat impersonation left |
| **3 Gaps** | ~~migration-replay~~ ✅ (v2.4) · ~~webhook auth negatives~~ ✅ (v2.3, folded into Phase 2) · ~~learning-loop design + tests (D4)~~ ✅ (v2.5) · ~~calendar/email/sync orchestration + D1~~ ✅ (v2.6) · ~~push tests~~ ✅ (v2.6) · ~~boundary-clock matrix~~ ✅ (v2.7) · ~~golden re-base~~ ✅ (v2.8, §10 corrected) · ~~API contract (OpenAPI)~~ ✅ (v2.9) · ~~health-as-L3 mapping~~ ✅ (v2.10, mapped not duplicated) · ~~security negatives~~ ✅ (v2.10) · ~~Flutter goldens~~ ✅ (v2.11) · ~~integration_test (X8)~~ ✅ (v2.13 — real emulator, caught + fixed the onboarding PageView-recreation bug) | every non-parked ❌ in §4 resolved; per-layer floors met; leak-guard table list covers every new surface |
| **4 CI evolve** | ~~Rewrite test.yml around `run_tests.py` (kill `-c /dev/null`); fast on push/PR; nightly scheduled; coverage artifact~~ ✅ (v2.12 — `-c /dev/null` gone; Flutter + goldens in the push gate; nightly runs `nightly --live --coverage`) · quarantine ledger → policy-on-demand (§15) | Gate runs on every push/PR + nightly; fast fits the job with headroom |

Phase-3 internal priority (by product risk — do them in this order):
1. ~~**Migration-replay**~~ ✅ (v2.4) — cheap, catches real breakage on every deploy.
2. ~~**Webhook auth negatives**~~ ✅ (v2.3) — security boundary with zero tests today.
3. ~~**Learning-loop**~~ ✅ (v2.5) — the product's core promise; hardest, started earliest.
4. ~~**Calendar/email/sync orchestration**~~ ✅ (v2.6) — 42 hermetic tests; D1 closed as mock-orchestration-only; fixed a latent `get_google_calendar_events` date-crash.
5. ~~**Push**~~ ✅ (v2.6) — the primary delivery channel: payload truncation, fan-out, silent push.
6. ~~**Boundary-clock matrix**~~ ✅ (v2.7) — 38 tests; `briefing_due_now` schedule matrix, briefing-mode + pulse_mode branches extracted pure, sentinel time windows; X7 closed.
7. ~~**Golden re-base**~~ ✅ (v2.8) — pytest golden surface for the briefing/classify/planner prompt stages; §10 corrected to the three-artifact model; re-based a stale planner pin (day-only-task routing).
8. ~~**API contract (OpenAPI)**~~ ✅ (v2.9) — pinned route surface + spec validity; found + fixed 8 duplicate operationIds (the GET+POST `api_route` split).
9. ~~**Health-as-L3 workflow mapping**~~ ✅ (v2.10) — mapped to health.yml +
   validate_deployment.yml (not duplicated); wrapper contract + fan-out isolation tested.
10. ~~**Security negatives**~~ ✅ (v2.10) — OTP attempt cap, rate limit,
    anti-enumeration, API-key fail-closed 503 / wrong-key 401 / disabled-user.
11. ~~**Flutter goldens**~~ ✅ (v2.11) — briefing-card + task-ack PNG goldens.
12. ~~**Phase 4 — CI evolve**~~ ✅ (v2.12) — `-c /dev/null` killed; push gate = fast tier incl. Flutter goldens; nightly wired.
13. ~~**integration_test (X8)**~~ ✅ (v2.13) — 2 on-device tests on the Pixel 8a AVD; **caught + fixed the onboarding PageView-recreation bug** (Sign-in step was unreachable on device); onboarding-nav regression tests added.
14. ~~**Phase-1 leftovers**~~ ✅ (v2.14) — teardown batching (X6, FK-safe
    parallel tiers) + per-layer cov floors (core+api from one measurement).
    + X1–X5 ledger.
15. ~~**Deferred ledger X2–X5**~~ ✅ (v2.15) — graph/edge undo-training
    (learn payloads on all 4 graph decision sites), confirm honesty
    (per-item `emit_confirmed_observation` on Telegram + API bulk confirm;
    frontend routes prefer learn_features), per-run chat/thread allocation
    (run_isolation.py), clean-slate pre-delete. **X1 decided: keep
    `projects`.** Bonus: leak-guard `raw_dumps.text`→`content` blind spot
    fixed. New tests: tests/unit/test_learning_ledger.py (7).

---

## 19. Deferred Ledger (discovered, not forgotten)

Items surfaced during the suite build and deliberately parked, each with
enough context that picking it up later is a decision, not archaeology.

| # | Item | Why deferred | Evidence / where it lives |
|---|---|---|---|
| X1 | `projects` table cleanup (~95% dead) | **DECIDED (v2.15): LEAVE AS-IS.** Live-DB audit (2026-08-15): table exists with **37 real rows** (not 12 — Zoho/Solvstrat/… from the 07-22 backfill); zero writers, `decisions.project_id` never populated; `project_organizations`/`organizations`/`people`/`entity_briefs` already dropped. It survived every cleanup because the test harnesses use it as the **live parent** for FK-orphan sweeps + seeds, and `graph_nodes.db_record_id → projects.id` still references it. Dropping = schema FKs (messages.linked_project_id SET NULL) + dashboard route (`/api/tasks/projects`, zero consumers) + harness rework — real break-risk, zero user value. Revisit only if it appears in a query plan again (then fold into a scoped migration, not a side-quest). Cheap hygiene kept on the table: the dead dashboard route + 2 diagnostic count-checks are candidates if a cleanup pass ever happens | backfill_graph.py:1846; apply_migrations.py:182; frontend/src/app/api/tasks/projects/route.ts; tests/sim/conftest.py + tests/uat/run_uat.py seeds |
| ~~X2~~ | ~~Graph/edge undo-training~~ | **CLOSED (v2.15)** — `record_decision` accepts `metadata`; all 4 graph decision sites persist `learn_features` (the EXACT emit_observation feature dict) + `learn_subsystem='entity_extraction'`, so the already-wired `emit_undo_correction` demotes on graph/edge undo. Contract-tested in test_learning_ledger.py (graph-shaped row → correction with exact features) | core/pulse/graph.py:613/726/773/880; core/decisions.py metadata param; closed v2.15 |
| ~~X3~~ | ~~`confirm_auto_all` observation is decorative~~ | **CLOSED (v2.15)** — Telegram `confirm_auto_all` + API `/api/auto-decisions/confirm` now emit PER-ITEM confirmations via `emit_confirmed_observation` (real subsystem + decision-time features, nothing when no learn payload); frontend verify/reject routes prefer `metadata.learn_features`; audit log reports trained count. No more decorative `auto_decisions` bucket | handler.py confirm block; api/index.py auto_decisions_confirm_route; frontend auto-decisions/[id]/verify+reject; closed v2.15 |
| ~~X4~~ | ~~Per-run chat allocation (§7)~~ | **CLOSED (v2.15 + v2.16)** — (1) v2.15: per-process chat band (9.1M–9.99M, distinct from every legacy fixed id) + per-run thread UUIDs; consumers switched (sim seed, note_capture, suite2, UAT); leak guard knows legacy ids + the run band. (2) v2.16: the documented residual (marker-title sweeps crossing truly-concurrent runs) closed with a **cross-machine Redis lock** — `acquire_sandbox_lock()` SET NX EX via the existing Upstash client; second live run fails fast with holder info; TTL self-expires on killed runs; release never clears another run's token; Redis-less envs skip it. Verified live | tests/fixtures/run_isolation.py; closed v2.16 |
| ~~X5~~ | ~~Clean-slate pre-delete (§7)~~ | **CLOSED (v2.15)** — session-start purge of test-tenant marker rows (owner-scoped, children-first) + chat/thread-keyed test rows, so a killed run's residue can't poison the next run. Verified live: purged 15 residual `[TEST]` graph_nodes. Non-test-tenant rows deliberately untouched — the fail-closed guard stays the enforcement point. **Bonus fix:** leak guard's `raw_dumps.text` → `content` (column never existed — raw_dumps leaks were unflagable) | tests/conftest.py `_clean_slate_before_live_session`; tests/fixtures/run_isolation.py; closed v2.15 |
| X3 | `confirm_auto_all` observation is decorative | The confirm path emits `subsystem='auto_decisions'` with `features={'count': N}` — a pattern bucket nothing reads (no compute_pattern_confidence call targets `auto_decisions`), so "patterns strengthened" is currently an overclaim. Proper fix: look up each confirmed decision's learn_features and emit per-item confirmed observations against the real subsystem | handler.py:180-190 |
| X4 | Per-run chat allocation (§7) | Fixed `_TEST_CHAT_IDS` still shared by CI + local + manual phone; CI↔local race on the shared sandbox remains. Per-session unique chat_ids seeded into users.telegram_chat_id kills it | conftest.py:82; sim 999999999 / 9000000+offset |
| X5 | Clean-slate pre-delete (§7) | Only the post-session leak guard exists; a killed run still poisons the next run's guard until a seed run cleans up. Pre-session purge of test-tenant marker rows makes runs self-healing | conftest.py:140-151 (no pre-delete) |
| ~~X6~~ | ~~Teardown batching~~ | **CLOSED (v2.14)** — sim teardown rewritten as FK-safe parallel tiers (children-before-parents, verified against the live FK graph; NO ACTION edges org_creation_signals→tasks/raw_dumps pinned to tier 0), each tier's deletes concurrent via ThreadPoolExecutor, owner-scoped unchanged (leak-safety intact). Module sweep + per-test cleanup + FK-orphan passes all batched | tests/sim/conftest.py; deferred from Phase 1, closed v2.14 |
| ~~X7~~ | ~~`delete_calendar_instance` + `get_upcoming_calendar_events` untested~~ | **CLOSED (v2.7)** — folded into the #6 boundary-clock work: frozen-clock 14-day bounds test + instance-delete guards now in test_google_orchestration.py | google_service.py (both) |
| ~~X8~~ | ~~Flutter integration_test~~ | **CLOSED (v2.13)** — 2 on-device tests (`integration_test/app_flow_test.dart`) on the Pixel 8a AVD: fresh-install boot → welcome (5 personas), persona pick → Continue → Sign-in, Back. While building it, caught + fixed the **onboarding PageView-recreation bug** (Sign-in step unreachable on device); regression-pinned in test/onboarding_nav_test.dart. `make test-app-integration`; needs a booted emulator | rhodey_app/; deferred from v2.11, closed v2.13 |

**Definition of done (whole plan):** `run_tests.py all` is green on a clean
checkout and fits its budgets; any aspect is individually runnable; §4 has no
un-parked ❌; a new feature or bug fix lands with its aspect tagged + tested;
the flaky ledger is empty or dated.

---

## 18. Known Deliberate Non-Goals

- Load/chaos artillery beyond existing breaker/rate-limiter coverage.
- Backwards-compat pinning of retired flows (clarifier, entity mirrors).
- Per-aspect coverage thresholds (gamed; marker lint is the anti-zero rule).
- Dashboard visual-design testing — parked with the admin-panel decision
  (§16 D3); if it becomes an admin panel, logic tests + one smoke E2E only.
- Mutation testing as a gate — the marker lint + coverage floors are the
  strength signal; mutation runs are not worth the runtime for a solo dev.
- A separate QA/UAT workflow or team-facing harness — there is no team; L4
  is the author's own regression net.
