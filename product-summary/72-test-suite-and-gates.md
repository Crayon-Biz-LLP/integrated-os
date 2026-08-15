# 72. Test Suite & Gates

> Verified 2026-08-15. Full plan: `plans/75-comprehensive-test-plan.md`.
> Running the suite: `tests/README.md`.

## What exists

- **865 Python tests** across 5 layers + **62 Flutter tests** (including 2
  on-device integration tests on the Pixel 8a emulator).
- **13 aspect markers** (`pulse`, `briefing`, `sentinel`, `decision`, `ingest`,
  `webhook`, `auth`, `calendar`, `email`, `sync`, `retrieval`, `graph`,
  `app`) — strictly registered in `pytest.ini`; every test module carries one
  primary marker, so any aspect is runnable with `-m <aspect>`.
- **Marker-presence lint** (`scripts/check_marker_presence.py`) — a new/modified
  test file must carry an aspect marker, or the gate fails (this is the rule
  that prevents "a new feature lands with 0% tests on its aspect").

## Layers (L0–L4)

| Layer | What | When |
|-------|------|------|
| L0 | Unit tests (no DB, no network) | fast |
| L1 | Hermetic fakes | fast |
| L2-mock | sim/cluster suites with mocked integration | fast |
| L2-live | Real-DB variants (real rows, real schema) | nightly |
| L3 | API contract, golden, orchestration, security negatives | nightly |
| L4 | UAT — 22 absorbed scenarios driving real code paths | nightly |

Pacing-sleep tests and live-DB tests are nightly by definition (a rate-limiter
sleep would detonate a 5-minute fast budget).

## The runner & gates

- **`scripts/run_tests.py`** — the only entry point: `--tier fast` (≈5 min:
  L0 + L1 + L2-mock + app) and `--tier nightly` (≈20 min: L2-live → L4 +
  coverage + leak guard). `--inventory` reports quarantined/skipped debt.
- **Pre-push hook** runs the fast tier; CI rewired around the runner
  (`.github/workflows/test.yml`, `nightly.yml`, `validate_deployment.yml`) —
  the old `pytest -c /dev/null` invocation is dead.
- **Migration replay** — every migration in `db/` is replayed from scratch
  against a fresh schema (94/94 green) so the chain can't rot.
- **Per-layer coverage floors** enforced by the nightly tier.

## The live-sandbox contract (why concurrent runs are safe)

1. **Test tenant only** — live suites run as the dedicated "Test" user
   (`tests/fixtures/test_tenant.py`); never the channel tenant. No test tenant →
   suites skip, fail-closed.
2. **Per-run chat allocation** — `tests/fixtures/run_isolation.py`: each process
   gets its own chat band (9.1M–9.99M) + thread UUIDs, so PK collisions are
   structurally impossible (X4).
3. **Cross-machine Redis lock** — `acquire_sandbox_lock()` (Upstash `SET NX EX`,
   TTL 45 min) serializes live runs: nightly cron vs local can't race the
   marker sweeps. Second run fails fast with holder info; killed runs
   self-heal via TTL (X4 residual).
4. **Clean-slate pre-delete** — session start purges test-tenant marker rows
   (owner-scoped, children-first) + chat/thread-keyed rows, so a killed run
   doesn't poison the next one (X5).
5. **Fail-closed leak guard** — `tests/conftest.py` fails the session if any
   `[TEST]`-marked row is owned by a non-test tenant (with a fixed
   `raw_dumps.content` column name so the guard actually sees leaks).

## The suite earns its keep

Bugs the suite caught during the build: the onboarding Sign-in unreachable on a
real device, 8 duplicate OpenAPI operationIds, the undo-training trust-breaker,
migration-chain breakage, a calendar date crash, a stale planner golden, and the
leak-guard `raw_dumps.text` blind spot.
