# Scheduler Ownership Map

**One scheduler owns each heartbeat. No duplicates.** This document is the
authority on who fires what — if you add a job, put it in exactly one place.

## The two schedulers

| Scheduler | What it hits | Why |
|---|---|---|
| **cron-job.org** | Modal web endpoints (`/api/*`) | Real-time, user-facing heartbeats. Always-warm Modal container, precise `:00`/`:30` timing, per-tenant isolation available (Modal workers). |
| **GitHub Actions** | Direct job code (runs Python on the runner) | Batch/maintenance jobs. Delay-tolerant, generous timeouts (10–45 min), **never** touch the Modal web ceiling. |

Plus one **Modal-native schedule**: `beeper_bridge_sync` (60s) — a scheduled
Modal function, not a web request.

## Ownership table

| Job | Scheduler | Endpoint / workflow |
|---|---|---|
| Pulse briefing (30 min) | **cron-job.org** | `/api/pulse-cron` — fan-out to per-tenant `brief_tenant` Modal workers (900s each) |
| Sentinel nudge (5 min) | **cron-job.org** | `/api/sentinel` |
| Decision pulse (30 min) | **cron-job.org** | `/api/decision-pulse` |
| Notes roundup (2× daily) | **cron-job.org** | `/api/roundup` |
| Beeper bridge (60s) | **Modal** | `beeper_bridge_sync` |
| Email ingest (Gmail/Outlook) | **GHA** | `email_ingest.yml` |
| Journal + graph ingest (incl. graph backfill) | **GHA** | `ingest.yml` (runs `backfill_graph` before each pulse slot) |
| Call recording ingest | **GHA** | `call_ingest.yml` |
| Retrieval index backfill | **GHA** | `retrieval_backfill.yml` |
| Clean duplicate nodes | **GHA** | `clean_duplicate_nodes.yml` |
| Concept sweep | **GHA** | `concept_sweep.yml` |
| Knowledge synthesis | **GHA** | `synthesis.yml` |
| Persona synthesis | **GHA** | `persona_synthesis.yml` |
| Memory clusters / orphan cleanup | **GHA** | `memory_clusters.yml` |
| Renew Drive watch channel | **GHA** | `renew_drive_channel.yml` |
| System health check | **GHA** | `health.yml` |

**Retired (Sep 2026):** `backfill_graph.yml` (graph backfill retired — one-time script replaces it),
`dedupe_pending.yml` + `core/skills/dedupe_pending.py` (duplicate cron of the inline approval-path
merge proposal — proposals lacked origin linkage and never resolved), `research_worker.yml` (research
agent no longer scheduled), `validate_deployment.yml` (auto-trigger broke after the CI workflow rename;
removed), `diag-connectivity.yml` (throwaway diagnostic).

## Rules (enforced by history, not by code)

1. **Never add a `schedule:` to a GHA workflow for a job cron-job.org already
   fires.** The pulse (`pulse.yml`) and sentinel (`sentinel.yml`) schedules were
   removed after both schedulers fired them in parallel — the sentinel showed
   mixed `cron` + `cli` triggers on `pulse_runs`, proving duplicate fire.
2. **Batch jobs live on GHA on purpose.** They are delay-tolerant, run with
   their own timeouts, and never hit the Modal web function ceiling. Moving
   them to cron-job.org adds complexity with zero benefit.
3. **User-facing heartbeats live on cron-job.org on purpose.** Precise timing
   and the always-warm Modal container. If one becomes heavy (multi-tenant
   LLM work), apply the **per-tenant worker pattern** (like `brief_tenant`):
   the web endpoint fans out to one Modal function per tenant — each with its
   own timeout — instead of running tenants sequentially in the request.
4. **If you repoint a job, update this table.** A stale map is how duplicates
   sneak back in.
