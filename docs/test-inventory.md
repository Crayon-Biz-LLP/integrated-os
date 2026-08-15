# Test Inventory — Phase 0 Report (plans/75 §17)

Baseline measured **2026-08-15**. Reconciliation of plans/75 §4 (surface ×
coverage) against reality, and the numbers the plan's budgets were waiting
on.

---

## 1. Test census (stated convention: `def test_` / `async def test_`)

| Surface | Count | Notes |
|---|---|---|
| Backend `tests/unit` | 570 functions | mocked DB/LLM/clock |
| Backend `tests/sim` | 95 functions | simulated flows |
| Backend `tests/clusters` | 39 functions | cross-system clusters |
| Backend `tests/tenants` | 7 functions | tenant isolation |
| Backend root (`test_api.py`, `test_retrieval.py`, …) | 42 functions | — |
| **Backend total** | **753 functions** | 706 pass + 120 skip (mock mode) |
| App `rhodey_app/test/` | 11 `testWidgets` (56 tests total) | widget tests only |
| Dashboard `frontend/` | **0** | lint-only (parked, plan §16 D3) |

## 2. Mock-mode baseline (today's "fast", pre-split)

```
pytest tests/ -q            → 706 passed, 120 skipped, 2 warnings
wall-clock: 28–30s          exit 0
```

The 120 skips are the live-gated share (see §3). Baseline is **green**.

## 3. Live-gated share (the L2-live population)

| Dir | Total | Skip in mock | Live-only |
|---|---|---|---|
| sim | 95 | 74 (78%) | 74 |
| clusters | 39 | 39 (100%) | 39 |
| tenants | 7 | 3 | 3 |
| unit | 570 | 4 (intentional sieve-noise skips) | 0 |

**113 of 134 sim+cluster tests are live-only today.** The L2-mock flavor
exists for only ~21 sim tests. Skip reasons: "live Supabase / test tenant
unavailable" (57) and "Requires LIVE_DB=true" (55); tenants additionally
need a `TENANTS_DSN` copy DB.

## 4. L2-live timing — the budget data

| Run | Result |
|---|---|
| `tests/sim` + `tests/clusters`, LIVE_DB=true | **> 10 min** (aborted at 600s) |
| `tests/sim/test_simulated_flows.py`, LIVE_DB=true | 9 passed in **35.7s** (~4s/test) |
| Slowest item | 11.2s **teardown** (one test) |

Extrapolated L2-live: **~8–12 min** for sim+clusters.

**Teardown dominates** (2–11s per test): per-test row deletion/cleanup, not
the assertions. This is the single biggest optimization target — a
session-scoped batch cleanup would cut minutes.

**Real LLM calls happen in live sim**: `tests/sim/conftest.py` and 3 test
files use `generate_content_with_fallback` — live-mode sim drives the real
pipeline including LLM. Another major cost driver.

**No residue after the interrupted run**: the aborted run left zero
`SIM_TEST` rows in the Test tenant — suites self-clean even when killed
(teardown cost buys safety; batch cleanup must preserve this).

## 5. Budget verdict (plans/75 §2.1)

- **Fast (5 min) = L0+L1+L2-mock+app**: today L2-mock is only ~21 tests;
  Phase 1 must create mock variants for the 113 live-only tests whose
  assertions survive mocking (per the §2.1 decision rule). Until then,
  fast is effectively L0+L1+21+app. **The invariant's Phase-1 scope is
  larger than tagging — it is the L2-mock build.**
- **Nightly (20 min) = L2-live+L3+L4+coverage+leak-guard**: L2-live alone
  is ~8–12 min; L3 (tenants/API/golden) + L4 (22 UAT scenarios, 4s pacing
  each + LLM) + migration replay + coverage **will likely exceed 20 min**.
  Mitigations to apply in order: teardown batching → L3 goldens to
  offline/pinned mode → trim UAT pacing → then move suites down a tier.
  The ceiling stays; scope shrinks (plan §2 rule).

## 6. Google credential trace (plan §16 D1 evidence)

`GOOGLE_REFRESH_TOKEN` is consumed by:
- `core/services/google_service.py` — the real Google API client
- `scripts/sync_notebooklm_docs.py`

The CI secret therefore drives a **real Google API path** — the
"mock-orchestration-only vs real-API" decision is not blocked on credentials
existing; a `google_live` opt-in smoke is feasible today if wanted.

## 7. Workflow inventory (21) — entry points (plan §14.2)

| Workflow | Entry point |
|---|---|
| pulse | `python -m core.pulse_cli` |
| sentinel | `python -m core.pulse_cli sentinel` |
| decision-pulse | `python -m core.pulse_cli decisions` |
| ingest | `python -m core.skills.archive_ingest` + `backfill_graph` |
| email_ingest | `python -m core.skills.email_ingest` + `outlook_ingest` |
| call_ingest | `python -m core.skills.call_ingest` |
| backfill_graph | `python -m core.skills.backfill_graph` |
| retrieval_backfill | `scripts/run_backfill.py` |
| memory_clusters | `cleanup_orphans` + `scripts/run_memory_clusters.py` |
| concept_sweep | `scripts/concept_sweep_batch.py` |
| clean_duplicate_nodes | `scripts/clean_duplicate_nodes.py` |
| dedupe_pending | `python -m core.skills.dedupe_pending` |
| synthesis | `python -m core.skills.brain_synth_v2` |
| persona_synthesis | `python -m core.skills.persona_synthesis` |
| research_worker | `python -m core.agents.research_agent` |
| renew_drive_channel | `python -m core.skills.renew_drive_channel` |
| notebooklm-sync | (manual/dispatch) |
| flutter-distribute | `flutter pub get` + `flutter analyze` + build |
| **health** | `scripts/run_health.py --force` — ALREADY a CI gate |
| **validate_deployment** | `scripts/validate_deployment.py` — ALREADY a CI gate |
| **test** | `pytest` (mock or live) + `scan_tenant1_residue.py` |

**Map-don't-duplicate confirmed**: `health` and `validate_deployment` are
already gates; the M17 residue scan is already L0. Plan §14.2's disposition
holds — no parallel representations.

## 8. D2 evidence (production-project question)

The shared Supabase project carries Danny's real `telegram_chat_id`, Google
OAuth creds, and Gemini/OpenRouter keys as CI secrets. **The shared project
is production.** The Test-tenant-in-prod + leak-guard model is the deliberate
design; a separate test project was rejected for cost (plan §16 D2).

## 9. Implications for plan 75 (delivered to the plan)

1. **Phase 1's L2-mock build is a first-class deliverable**, not a side
   effect: ~113 tests need mock variants per §2.1; fast is otherwise ~21
   sim tests + L1.
2. **Nightly needs the teardown-batch optimization** before L3/L4 land, or
   the 20-min ceiling fails on L2-live alone.
3. **Live sim uses the real LLM** — those tests should carry
   `llm_live`-adjacent treatment (or pinned outputs) to keep nightly
   deterministic and cost-capped (plan §8.2).
4. **D1 is unblocked**: a `google_live` opt-in is feasible (real credential
   path exists in CI).
5. **UAT pacing (4s/classify × 22 scenarios + LLM) confirms §2.1's pacing
   rule**: L4 is nightly-only by construction.
