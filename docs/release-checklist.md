# Release Checklist — When to Run the Upgrade Gate

The upgrade gate (`.github/workflows/upgrade-gate.yml`, manual dispatch) runs
the **full stack in sequence** — fast tier + complete live suite (deep core,
sim cognitive, UAT L4, migrations replay, leak guard) against the TEST tenant
only, 240-min budget. It is the "is this safe to ship?" button.

This page answers **when** to press it. The decision is driven by **what
changed**, not by feel.

---

## 1. The rule of thumb

> **"If this change would be expensive to roll back, run the gate."**

A rollback is manual and disruptive. The gate costs up to an hour of CI.
Routine single-layer fixes don't need it — the push fast tier + that night's
nightly already cover them.

## 2. Trigger table

| Change class | Run gate? | Why |
|---|---|---|
| Routine fix / single-layer feature (one file area, no schema) | ❌ | Fast on push + nightly covers it |
| **New `db/` migration** | ✅ | Highest-risk class — schema affects every layer. Migration replay runs in nightly, but a full gate before a schema deploy is the safety net |
| Cross-layer feature (touches 2+ of: db, core, api, app) | ✅ | Multi-layer integration is where the suite earns its keep |
| Prompt / planner / classifier change | ✅ | Behavioral — changes how the LLM acts end-to-end. Weekly would catch it; you want it *before* ship |
| Multi-tenant / auth / RLS change | ✅ | The isolation matrix (tenants) + live checks — exactly what a tenant-leak bug needs |
| App upgrade (Flutter) | ✅ | Plus X8 on the emulator (device-level) |
| Deploy / infra change (Modal, workflows, env) | ✅ | Plus the health check *after* deploy |
| Big batch merge (squash of many changes) | ✅ | Batch risk compounds |

## 3. The cadence (and why each slot exists)

| Cadence | Question it answers | Can't be replaced by |
|---|---|---|
| **Fast — every push** (~1 min) | "Does this change break the hermetic suite?" | Mock-only; never touches the real DB |
| **Nightly — daily 01:00 UTC** (~4 min) | "Is the live stack healthy?" | The only thing exercising the real DB, schema, RLS, migration replay |
| **Weekly-deep — Sunday 01:30 UTC** (30–60 min) | "Do the LLM paths still work end-to-end?" | Real Gemini scenarios (sim + UAT L4); too slow/flaky to share the nightly budget |
| **Upgrade gate — on demand** (up to ~1h) | "Is this release safe to ship?" | Fires the full stack, sequenced, before a major deploy |

The gate does **not** replace the cadence — it's the fourth, on-demand slot.

## 4. The ceremony

1. **Trigger** — dispatch `Upgrade Gate — Full Suite` from
   GitHub Actions, with a note (release tag / feature / batch).
2. **Wait for green** — fast fails in ~1 min; deep live in ~5; sim/UAT last
   (Gemini-paced). Watch for the known S2 flakiness (plans/75) — a single
   failed scenario can be re-run rather than blocking the release.
3. **Deploy** — only on green. (Gate covers correctness; the deploy itself
   goes through the normal Modal path.)
4. **Post-deploy health check** — run `scripts/run_health.py --force` and
   confirm `scripts/validate_deployment.py` passes against the deploy
   timestamp. The health alert is Telegram-only by design (admin channel).
5. **Log the gate run** — note the dispatch in the release/PR so the
   verification is auditable.

## 5. Known limits (honest flags)

- **LLM nondeterminism**: the S2 scenario carries residual real-Gemini
  variance (occasional fail→pass→fail). The gate's green is *conditional* on
  that — a single rerun is the documented remedy, not a patch.
- **Weekly-deep cadence**: LLM-path breakage can go undetected up to 7 days
  between Sunday runs; the gate is the way to compress that window before a
  ship.
- **The gate shares the X4 sandbox lock**: don't run it while a nightly or
  weekly live run is active — a concurrent live session fails closed.
