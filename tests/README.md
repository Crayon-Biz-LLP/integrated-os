# Test Suite — Contract & Conventions (plans/75)

Single source of truth for how this suite runs, what it may touch, and the
rules every new test must follow. The governing design document is
`plans/75-comprehensive-test-plan.md`; this README is the operator-facing
summary.

## Running

Use the unified runner — never raw `pytest` for a gate decision:

```bash
python scripts/run_tests.py fast            # L0+L1+L2-mock+app — no DB, ≤5 min
python scripts/run_tests.py nightly --live  # L2-live+L3+coverage+leak guard, ≤20 minpython scripts/run_tests.py pulse            # ONE aspect, any layer (mock)
python scripts/run_tests.py pulse --live    # that aspect, live (TEST tenant)
python scripts/run_tests.py e2e --live      # L4 — the 22 UAT scenarios alone
python scripts/run_tests.py --layer unit    # tier filter
python scripts/run_tests.py --inventory     # Phase-0 report
make test-fast / test-nightly               # Makefile aliases
```

The `.githooks/pre-push` hook runs `fast --no-app` before every push. CI runs
fast on push/PR; the deep suite runs nightly (`.github/workflows/nightly.yml`).

## Aspect markers — the anti-0% rule

Every test module carries exactly ONE primary aspect via module-level
`pytestmark` (exclusive-primary; `-m <aspect>` selects it, `-m "a and b"`
intersects):

```
pulse · briefing · sentinel · decision · learning · ingest · webhook
auth · calendar · email · sync · retrieval · graph
```

plus opt-in cost markers: `llm_live` (real LLM calls), `google_live` (real
Google API). Registered in `pytest.ini` with `--strict-markers` — unknown
markers fail collection.

- **New/modified test files MUST carry an aspect marker** — enforced by
  `scripts/check_marker_presence.py` (L0, runs in fast). A test you can't
  select by aspect is invisible to the suite.
- Ops surfaces (rate limiter, providers/failover) are exempt by design —
  covered by per-layer floors, not aspects.
- `app` (Flutter) and `dashboard` (frontend) are separate runtimes — never
  pytest markers. Orchestrated by the Makefile.

## Env contract — never silently hit production

The shared Supabase project IS production (plan §16 D2). Live layers run
against it **only** with explicit opt-in and the Test tenant only:

- `LIVE_DB=true` — required for live layers (runner sets it; refuses
  otherwise).
- `TEST_TENANT_UID` — explicit test tenant; else resolved by name
  (`TEST_TENANT_NAME`, default "Test", status active) via
  `tests/fixtures/test_tenant.py`. Never falls back to the channel tenant.
- `TEST_CHAT_IDS` — comma-separated chat ids admitted by the webhook gate
  IN ADDITION to the owner chat. **Default-off fail-closed** (plans/75 §7):
  unset/empty → no bypass, byte-identical to the legacy single-chat gate.
  Prod never sets it; only CI/nightly UAT runs do (UAT chat = `TEST_CHAT_ID`,
  default 909999999). The L4 suite drives `process_webhook` from that chat
  — it must be listed or every UAT message is rejected as Unauthorized.
- The runner **refuses** to run live against dummy/missing secrets and
  refuses to start nightly if the Test tenant can't resolve.
- Fast NEVER sets `LIVE_DB` (invariant, plan §2.1) — the runner refuses if
  it's already set.

## Cross-tenant leak guard

`tests/conftest.py` sweeps after the whole session: any `[TEST]`/`[SIM_TEST]`
marker row outside the test tenant fails the run. **Rule**: every new test
surface that touches a table must extend `_LEAK_MARKER_TABLES` (and the
chat-id patterns) in the same change — the guard only knows what it lists.

## Determinism policy (plan §8.2)

1. **Clock**: tests depending on `datetime.now()`/`today()` use the
   `frozen_clock` fixture (freezegun, Asia/Kolkata-anchored, Monday 09:30
   IST). Pulse windows, sentinel nudges, briefing-mode branches are
   deterministic, not wall-clock-flaky. For other instants, pass your own
   `freezegun.freeze_time(...)` anchored to `ZoneInfo("Asia/Kolkata")`.
2. **LLM**: three sanctioned mechanisms — (a) hermetic fakes at L1/L2-mock
   (default), (b) golden-output compares for prompt-shape assertions. No
   VCR/cassette infra (deliberate — goldens already pin responses). Tests
   that genuinely call the real LLM carry `llm_live` and are opt-in only
   (cost guard) — excluded from default nightly.

## Budgets (plan §2.1)

- Fast: ≤ 5 min, no live-DB tests. A suite that doesn't fit is moved down a
  tier (mock variant), never the ceiling raised.
- Nightly: ≤ 20 min. L2-live was the dominant cost (2–11s/test of teardown)
  — **teardown batching landed (X6, v2.14)**: sim deletes now run in
  FK-safe parallel tiers (children-before-parents, verified against the
  live FK graph), each tier's deletes concurrent via ThreadPoolExecutor,
  owner-scoped unchanged. See `docs/test-inventory.md` §5.

## Layers ↔ directories

| Layer | Where | DB |
|---|---|---|
| L0 static | runner steps (ruff, residue, marker lint) | no |
| (ops) migration replay | `tests/test_migrations_replay.py` — full chain on a throwaway local Postgres (initdb/pg_ctl; skips when absent) | scratch cluster |
| L1 unit | `tests/unit/` | mocked |
| L2-mock | sim/cluster tests with hermetic fakes | no |
| L2-live | `tests/sim/`, `tests/clusters/` (LIVE_DB) | TEST tenant |
| L3 integration | `tests/tenants/`, `tests/golden/`, root API tests | TEST tenant |
| L4 e2e/UAT | `tests/uat/test_uat_l4.py` (adapter over `run_uat.py`, 22 scenarios) | TEST tenant |

## Learning loop (vision #4) — the two-phase contract

The loop END is tested as a two-phase behavior delta (plans/75 §16 D4):

1. **Persist** — a decision flows through `emit_observation()` →
   `subsystem_patterns` rolling counter (write side).
2. **Re-run** — `compute_pattern_confidence()` on the SAME features returns a
   DIFFERENT recommendation than before the decisions (read side). The 3rd
   approved observation flips review → approve (MIN_PATTERN_OBSERVATIONS);
   corrections past MAX_ERROR_RATE=0.5 demote back.

Coverage:
- `tests/unit/test_learning_loop.py` (hermetic, no DB): escalation boundary,
  error-rate demotion, stateful two-phase emit→compute loop, undo-trains,
  fail-open. Uses a stateful fake client that accumulates subsystem_patterns
  rows like the real DB.
- `tests/sim/test_learning_loop_live.py` (L2-live, TEST tenant): real-DB
  two-phase flip + undo demotion against real pattern rows. The feature set
  is fixed; the fixture sweeps that exact (owner, subsystem, feature_hash)
  before/after so a killed run can't skew the counters.

**Undo trains (v2.5 fix).** The undo paths (Telegram `undo_auto_*`,
`/api/auto-decisions/undo`, `/api/decisions/undo`) used to reverse the
decision and re-pend the item with NO learning signal — the pattern that
caused a wrong auto-approve stayed strong and the same class kept getting
auto-approved (the "Not now that silently resets" trust-breaker). Now:
- Decision rows persist `metadata.learn_features` + `learn_subsystem` at
  record time (core/webhook/utils.py, core/webhook/email.py) — the EXACT
  features that were emitted, so a correction lands on the same pattern
  hash (rebuilding later would shift time-of-day dims and miss).
- `emit_undo_correction()` (core/webhook/utils.py) re-emits the INVERSE
  observation from every undo path: approval-undo → `corrected` (demotes),
  rejection-undo → `confirmed` (re-strengthens). Fail-open — a telemetry
  hiccup never breaks the undo.

**Ledger X2/X3 closed (v2.15).** Graph/edge decisions (core/pulse/graph.py)
now persist `metadata.learn_features` + `learn_subsystem` on all 4 decision
sites, so `emit_undo_correction` demotes the right pattern on graph/edge
undo. The bulk confirm paths (Telegram `confirm_auto_all`, API
`/api/auto-decisions/confirm`) emit PER-ITEM confirmations against each
decision's real subsystem + decision-time features via
`emit_confirmed_observation` — no more decorative `auto_decisions` bucket;
the frontend verify/reject routes prefer `metadata.learn_features` too. See
plans/75 §19.

**Sandbox isolation (v2.15/v2.16, ledger X4/X5).**
`tests/fixtures/run_isolation.py` gives every pytest process its own chat-id
band (9.1M–9.99M) + per-run thread UUIDs, so CI + local + the manual phone
never collide on the fixed ids the sandbox used to share (the leak guard
knows both the legacy ids and the run band). A **cross-machine Redis lock**
(`acquire_sandbox_lock`, SET NX EX via the existing Upstash client)
serializes live sessions — the second concurrent run (e.g. nightly cron vs a
local run) fails fast with holder info instead of racing; TTL self-expires
on a killed run; release never clears another run's token; Redis-less
environments skip it. A session-start clean-slate pre-delete purges
test-tenant marker rows so a killed run's residue can't poison the next
run's leak guard; rows owned by any other tenant are deliberately left for
the fail-closed guard. Fixed en route: the guard's `raw_dumps.text` marker
column never existed (`content` does) — raw_dumps leaks are now flaggable.

## Calendar / email / sync orchestration (v2.6)

Hermetic suites (no network, no DB — the D1 mock-orchestration-only default;
the real-API contract is gated behind the opt-in `google_live` marker):

| Suite | Aspect | What it pins down |
|---|---|---|
| `tests/unit/test_google_orchestration.py` | calendar | `sync_to_calendar` event-body construction (priority prefixes 🔥/☕/⚡, popup reminders 60+15, end=start+duration, timezone), insert-vs-patch routing, recurrence, **404 heal-and-reprovision** (null DB id, re-insert), non-404 propagation (never null the DB on a lie), no-creds skip, conflict check, day-scoped reads |
| `tests/unit/test_tasks_sync_orchestration.py` | sync | `sync_to_google`: completion patch on done/cancelled, insert-vs-patch, date-only vs datetime due, the 🕒 explicit-time IST title hack |
| `tests/unit/test_push_orchestration.py` | sync | `push_data_content` byte-aware truncation (emoji/CJK never split, empty/exact-fit passthrough); `send_push_notification` fan-out: token dedup (re-registered device ≠ double notify), android-high / ios-apns config, data stringification, 404 invalid-token cleanup owner-scoped; `send_silent_push` data-only payload |
| `tests/unit/test_email_send_orchestration.py` | email | Gmail draft send: In-Reply-To/References threading (+ threadId fallback), reply-all CC collection excluding sender+self, status→'sent' BEFORE the API call (double-send guard — a failed send stays 'sent', never blind-retried), legacy Subject-line strip; Outlook send: 202, 401 refresh-and-retry, no-token fail-clean |

Real bug caught in the sweep: `get_google_calendar_events` crashed on a
plain `date` (`.replace(hour=...)` TypeError → silently returned [] → dropped
calendar context). Now accepts both datetime and date (whole-day scope).

Deferred (ledger X7): `delete_calendar_instance` + `get_upcoming_calendar_events`
— the latter anchors on `datetime.now(UTC)`, so it folds into the #6
boundary-clock work. See plans/75 §19.

## Boundary-clock matrix (v2.7)

The time-dependent branches the plan's #6 clock item pins, all frozen-clock
or pure — no wall-clock flakiness:

| Suite | Aspect | What it pins down |
|---|---|---|
| `tests/unit/test_briefing_schedule.py` | pulse | `briefing_due_now` (the M9.7 schedule gate, previously script-only): window edges inclusive at ±15 min, weekday-vs-weekend slot separation, single-fire-per-heartbeat on the :00/:30 grid, midnight rollover (23:45 never fires a 00:00 slot), malformed-slot fail-closed, window clamp 1..15, `resolve_briefing_schedule` fail-closed→balanced, presets/picker sync |
| `tests/unit/test_briefing_mode_matrix.py` | briefing | `_resolve_time_intelligence` (extracted pure from `_process_pulse_impl`): Monday-morning boundary (10:59 vs 11:00), afternoon window 12:00–15:29 (incl. the 15:00/15:30 flip), Friday wrap-up, night wind-down ≥19:00, Friday ≥19:00 weekend entry, Sunday ≥19:00 pre-Monday precedence, and the documented midnight edge (hour < 12 includes 00:00 → "Morning check."); `_map_pulse_mode` every branch |
| `tests/unit/test_sentinel_time_windows.py` | sentinel | `get_recently_ended_events` post-meeting 5–30 min end-window filtering (boundary-inclusive, no-end-time skipped, wider fetch window by start) + `get_upcoming_events` now→now+ahead bounds |

Clock policy: `datetime.now()`-dependent tests use the shared `frozen_clock`
fixture or an explicit `freezegun.freeze_time(...)` anchored to a timezone
(Asia/Kolkata for IST logic, UTC where the code anchors UTC) — see
`tests/conftest.py`. `get_upcoming_calendar_events` + `delete_calendar_instance`
(frozen-clock 14-day bounds + instance-delete guards) live in
test_google_orchestration.py; deferred-ledger X7 closed.

## Golden artifacts (v2.8) — three classes, one rule

Rule (plans/75 §10): **no pytest golden depends on a real tenant's live
data — every golden is hermetic** (mocked fixtures, never a live DB read).

| Artifact | Class | Consumed by |
|---|---|---|
| `golden/briefing_tenant1.txt` · `classify_tenant1.txt` · `planner_tenant1.txt` | Channel-tenant regression pins — tenant #1's pinned OUTPUT shape (the `_tenant1` name is intentional: a prompt refactor that changes his output fails the gate). Hermetic: reproduced under mocked rows/graph/tz | `test_briefing_prompt_golden.py` (briefing) · `test_classify_prompt_golden.py` (ingest) · `test_planner_prompt_golden.py` (decision) + manual `scripts/verify_m9_*.py` |
| `golden/whatsapp_classify/golden.json` | Hand-labeled input corpus (real threads as fixtures, not tenant output) | `test_whatsapp_golden.py` (ingest) |

Regenerate a stale pin from the current render with the SAME fixtures the
test uses, review the diff, commit pin + test together — never a silent
overwrite. History: v2.8 re-based `planner_tenant1.txt` (2 intentional
lines: day-only-task deadline routing); the briefing/classify pins
reproduced clean. See `tests/golden/README.md`.

## API contract (v2.9) — pinned surface + spec validity

`tests/unit/test_api_contract.py` (ops surface — exempt from aspect lint):

- **Route inventory pin**: the exact 78-path / 89-operation surface of
  `api.index.app`, committed as `PINNED_ROUTES` and exact-matched against the
  live app. Adding OR removing a route fails the gate until the pin is
  updated deliberately — an accidental route rename can never silently break
  the app. (Regenerate with the one-liner in the file docstring.)
- **Spec validity**: OpenAPI 3.x metadata, **unique operationIds** (the hard
  rule), every registered route documented.

**Fixed a real bug on the way**: the 8 `@app.api_route(path,
methods=["GET","POST"])` routes (pulse-cron, sentinel, decision-pulse,
maintenance, health, admin/spend, roundup, beeper-sync) each produced ONE
operationId for both methods (FastAPI's `generate_unique_id` takes
`list(route.methods)[0]`) → 8 duplicate operationIds → invalid OpenAPI for
strict consumers. Split into explicit `@app.get` + `@app.post` decorators;
routing behavior verified unchanged by the contract test + existing API
tests. Spec is now valid OpenAPI 3.1.0, 0 duplicates.

## Health-as-L3 + security negatives (v2.10)

**Health/validate is mapped, not duplicated** (plan §14.2): `health.yml`
→ `scripts/run_health.py --force` → `run_full_health_check()` and
`validate_deployment.yml` (post-CI Modal validation) ARE the L3 health
surface. `tests/unit/test_health_wrapper.py` (ops-exempt) pins the wrapper
behavior the workflows rely on: `is_business_hours()` boundary matrix
(UTC 03:00–17:00 = IST 08:30–22:30), CLI contract (skip outside hours,
silent-when-clear, issues → Telegram alert + exit 1, `--force` bypass),
and the M6 fan-out failure isolation.

**Security negatives** — `tests/unit/test_auth_negatives.py` (auth aspect):

- OTP: 5-attempt brute-force cap (burn-one-try increments BEFORE
  validation; no burn at the cap), 60s resend rate limit, expiry,
  consumed-code rejection, the **anti-enumeration parity rule** (unknown
  email / no code / wrong code / reused code all return the identical
  message), daily send cap, disabled-user never emailed.
- API keys: unknown key → None (incl. table-missing fail-closed),
  `require_api_auth` **fail-closed 503** when `API_SECRET_KEY` unset (no
  `ALLOW_DEV_AUTH`), 401 on wrong key, per-user key tenant scoping,
  disabled-user key never scopes.

## Flutter goldens (v2.11)

`rhodey_app/test/goldens/` pins the app's VISUAL contract as committed PNGs
(`matchesGoldenFile`): the briefing card in the exact Aug-10 shape that broke
(bold `**Work**` headers → body rendered twice + literal asterisks) + the
task-ack card. A layout/rendering change is now a reviewed golden update
(`make test-app-goldens` regenerates; `make test-app` verifies), never a
silent regression. App suite: 62 tests (58 widget + 2 goldens + 2 onboarding
nav regression).

Note: widget goldens render with the test framework's default Ahem font
(solid blocks) — they pin LAYOUT/STRUCTURE. Real-font/API/gesture coverage is
integration_test (X8, closed v2.13).

## Flutter integration_test (X8 — v2.13, closed)

`rhodey_app/integration_test/app_flow_test.dart` (2 tests) drives the REAL
app binary on a booted Android emulator/device (`flutter test
integration_test -d <emulator>`; alias `make test-app-integration`):
fresh-install boot → onboarding welcome (all 5 persona cards), persona pick
→ Continue → Sign-in step, Back navigation. Real platform channels, real
prefs/secure storage, real Firebase init.

**While building it, a real production bug was caught + fixed**: the
onboarding PageView was recreated mid-animation whenever `_page` became > 0
(progress bar / bottom bar slots change type between pages; no key →
reconciliation destroyed the PageView, cancelling `animateToPage` and
snapping back to page 0 — the Sign-in step was unreachable on device). Fix:
stable `ValueKey('onboarding-pages')` + always-rendered progress-bar slot;
regression-pinned in `test/onboarding_nav_test.dart` (element-identity check)
+ `test/pageview_animation_test.dart`.

**Emulator note**: on memory-constrained hosts, launch with a small guest RAM
(`-memory 1536 -no-window -gpu swiftshader_indirect`) — the default 4GB
config gets OOM-killed.

## CI (Phase 4 — v2.12)

- **Push/PR gate** (`.github/workflows/test.yml`): the fast tier through the
  runner — `-c /dev/null` is gone (that flag discarded pytest.ini, so CI
  respected no markers/skips). Fast = L0 (ruff + residue + marker lint) +
  L1/L2-mock + **app**: Flutter is set up in CI (version-pinned 3.44.5 so
  goldens stay SDK-stable) + `flutter pub get`, so the app's 62 tests incl.
  the briefing-card/task-ack goldens gate every push.
- **Nightly** (`.github/workflows/nightly.yml`): `run_tests.py nightly
  --live --coverage` with `TEST_CHAT_IDS` for the L4 UAT scenarios +
  **per-layer coverage floors** (v2.14): one pytest-cov measurement,
  enforced per source layer via `coverage report --include` — `core/*`
  under `COV_FLOOR` and `api/*` under `API_COV_FLOOR` (defaults 20,
  env-configurable, fail-closed on 0%). Migration replay runs inside the
  same session (tests/ root; postgres auto-discovered on ubuntu).
- The pre-push hook runs `fast --no-app` locally (dev machine has Flutter
  deps cached; CI runs the full fast tier incl. app).

## Migration replay (ops)

- `scripts/replay_migrations.py` proves db/01..db/101 apply IN ORDER to a
  fresh schema (numeric order — lexicographic would run db/100 before db/10).
- Base: `db/00_replay_base.sql`, GENERATED by `scripts/generate_replay_base.py`
  from the newest backups/ dump minus chain-created objects (the chain is
  not self-contained: `tasks`/`memories`/`graph_nodes`/… were created in the
  editor pre-chain) + a hand supplement for pre-chain tables dropped
  mid-chain. Regenerate after the chain or backup changes — never hand-edit.
- Failure classification: structural breaks (missing objects, type clashes,
  duplicate creates) FAIL the replay; data migrations that reference
  pre-chain rows (db/06) are reported as expected-on-empty-base skips.
- Already paid for itself: db/101 dropped retrieval_triples while db/04's
  retrieval_edges FK'd to it — a fresh replay failed until the FK drop was
  added (IF EXISTS no-op on prod).
- Ops surface — no aspect marker; runs wherever postgres binaries exist
  (local + ubuntu CI runners).

## UAT (L4) specifics

- `tests/uat/run_uat.py` holds the 22 scenario bodies; `test_uat_l4.py` is a
  thin pytest adapter (wrap, don't rewrite — plans/75 §13): session-scoped
  TEST-tenant wrapper, outbound-mock patchers, owner-scoped `[UAT]` cleanup,
  per-scenario aspect marks (exclusive-primary per test).
- The webhook gate admits the UAT chat via `TEST_CHAT_IDS` (see env
  contract). The harness no longer impersonates the owner chat — the
  legacy impersonation is archived (scripts/archive/README-uat.md).
- Scenario 17 (health check) tolerates documented pre-existing conditions
  (`LLM fallback`, `NULL embeddings` — indexing defaults OFF per
  core/retrieval/config.py:19); the full report is captured in its details.
- UAT pacing is 0.5s/classify: the Gemini limiter self-paces via
  `acquire_async` (core/lib/rate_limiter.py), so a long sleep was redundant
  wall-clock tax.
