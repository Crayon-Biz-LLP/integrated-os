# Session 74 — Aug 10–14 Wave: Security, Hardening, Beeper, App Surfaces

**Date:** Aug 10–14, 2026 · Commits: the `fix`/`feat`/`build`/`perf`/`fix(pulse)`/`fix(beeper)`/`fix(ingest)`/`feat(app)` run between the security audit and the test-suite build.

## What shipped (in order)

1. **Security audit + auth hardening (Aug 10–11)** — DB grants reworked (db/87–91: anon revoked, per-tenant roles); API surface re-audited; `require_api_auth` narrowed to per-user key hashes + legacy shared key.
2. **Action-pipeline hardening (Aug 13)** — typed contracts (`core/actions/models.py` per-op subclasses), PATCH semantics (deltas only, never write None — `executor.py` `_build_patch` builders), deterministic timezone-anchored scheduling, provider-shape validation, learning-loop wiring on executed actions.
3. **Rate-limiter self-feeding starvation fix** — sentinel workloads got their own limiter pool (`sentinel_flash_limiter` in `core/lib/rate_limiter.py`) so a sentinel burst can't starve pulse briefings (and vice-versa). Documented in `product-summary/62-pulse-outage-rate-limiter-starvation.md`.
4. **Beeper cutover (Aug 11)** — WhatsApp capture path moved from MacroDroid to the Beeper bridge (`core/skills/beeper_ingest/beeper_send/beeper_desktop` + `docs/beeper-messaging-layer.md`); cursor/token/room-map fixes + liveness alert.
5. **Direction-awareness (Aug 14)** — own-sends (email/Teams/Outlook) never surface as inbound items; `direction` field flows through `raw_dumps`/`messages`/`tasks`.
6. **App approval surface (Aug 11–14)** — Quick Confirmations (type filter, selection-mode batch approve/reject), channel batch approve, real priority buckets, draft editing, voice-rendered acks, Telegram-independent reply path (`reply_delivery.py`/`message_voice.py`).
7. **Per-item undo (feat(app))** — manual approve/reject reversible with side-effect reversal through the `undo_*` decision flow; undo is a ledger decision (`decisions.superseded_by` chain).
8. **Build/deps** — Dependabot resolution, `app_version` upsert fix (db/78 constraint), perf batching of approve/reject (parallel LLM pipelines + no-retry contract).
9. **Ingest fixes** — Teams/Email/Outlook own-sends suppressed; Beeper VPS/Desktop capture path fixes; day-only tasks route to Google Tasks without a 9am calendar block.

## Learning-loop groundwork
The `decisions` ledger + `subsystem_patterns`/`subsystem_telemetry` tables (db/100 `decision_action_ledger`) landed in this window — the foundation the test-suite session's X2/X3 work built on.

## Follow-ups absorbed into the next session
- Test-suite build (session 73) used this wave as the baseline.
- Docs re-baseline (plans/76) mapped every commit in this window to a coverage item.
