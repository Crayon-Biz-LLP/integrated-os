# 4. Backend & Frontend

> Updated 2026-08-15. Module trees below are the live filesystem, not
> aspirational. The legacy `core/pulse/engine.py` is **gone** (replaced by
> `briefing.py` + the decision pipeline); the old rewrite/proxy layer is gone
> with the platform move to Modal.

## Backend (Python/FastAPI)

### Entry Point
A single FastAPI application at `api/index.py` handles all HTTP traffic,
deployed via `infra/modal_app.py` on **Modal** (Python 3.11). Routes serve
Telegram webhooks, the Pulse briefing engine, `/api/` endpoints for the app
(auto-decisions, confirm/reject/undo, pulse-cron, inbox feed), health checks,
and diagnostics.

### Module Architecture

```
api/index.py                      — FastAPI app, auth middleware, app API routes
core/
├── webhook/                      — Real-time channel processing
│   ├── handler.py                — Main webhook: dedup, auth (bound tenant chat + TEST_CHAT_IDS), routing
│   ├── email.py                  — Email/Teams/Outlook ingest (direction-aware)
│   ├── classify.py               — Gemini intent classification
│   ├── dispatch.py               — Route by intent, interrogate_brain, all handlers
│   ├── commands.py               — Telegram command handlers (incl. /why, /undo)
│   ├── multimodal.py             — Image/audio/document processing
│   ├── telegram.py               — Telegram API send wrapper + FCM push trigger
│   ├── workflows.py              — Workflow state engine
│   ├── feedback_loop.py          — confirm/correct feedback for auto-decisions
│   ├── graph.py                  — graph decision callbacks (pe/g shortcodes)
│   ├── utils.py                  — pending-decision processor, confirm_auto_all, emit_confirmed_observation
│   └── why_handler.py            — /why decision audit handler
├── actions/                      — Unified Action Planner
│   ├── planner.py                — LLM resolves intent into typed Action objects
│   ├── executor.py               — Typed contracts, PATCH semantics, parallel pipelines, no-retry
│   └── models.py                 — Action/Operation dataclasses
├── pulse/                        — Scheduled intelligence engine
│   ├── briefing.py               — Single LLM call, parallel context assembly, write-behind
│   ├── decision_pulse.py         — AI-free pending approvals
│   ├── sentinel.py               — Meeting alarms + piggyback maintenance jobs
│   ├── pipeline.py               — Consolidated health monitor
│   ├── graph.py                  — Knowledge-graph decisions + learn-feature persistence
│   ├── entity_extractor.py / entity_resolver.py — entity extraction + deterministic resolution
│   ├── patterns.py               — practice/pattern detection lifecycle
│   ├── practices.py              — practice detection (wired at briefing)
│   ├── memory.py / memory_clusters.py / cluster_discovery.py — memory + clustering
│   ├── calendar.py               — Google Calendar/Tasks sync
│   ├── tools.py                  — create_task_direct, create_note_direct, update_task_status
│   ├── context.py                — ContextProvider: TTL caches, hydrate_* functions
│   ├── maintenance.py            — maintenance jobs
│   ├── models.py                 — PulseOutput, BriefingContext data contracts
│   ├── run_logger.py             — Pulse run tracking
│   └── utils.py                  — Formatting, routing context
├── decisions.py                  — decision ledger + learning loop (record_decision, pattern training, undo corrections)
├── agents/                       — Autonomous workers
│   ├── research_agent.py         — Jina AI web search + dossier
│   └── cleanup_orphans.py        — orphan sweep
├── skills/                       — Batch scripts (CI-run)
│   ├── archive_ingest.py / email_ingest.py / outlook_ingest.py / teams_ingest.py / call_ingest.py
│   ├── beeper_ingest.py / beeper_send.py / beeper_desktop.py   — WhatsApp via Beeper (replaced MacroDroid)
│   ├── backfill_graph.py / brain_synth_v2.py / persona_synthesis.py
│   ├── whatsapp_ingest.py        — legacy MacroDroid path (superseded)
│   ├── dlq_consumer.py / dedupe_pending.py / renew_drive_channel.py / outlook_token_helper.py
├── services/                     — Shared services
│   ├── db.py                     — TenantAwareClient, tenant_table(), tenant_scope, owner_id injection
│   ├── async_db.py               — async client variant
│   ├── auth.py / otp_email.py    — Google / email-OTP sign-in
│   ├── persona.py / persona_verifier.py — persona layer
│   ├── onboarding.py             — onboarding state machine
│   ├── briefing_refresh.py / briefing_schedule.py / briefing_sections.py — silent push briefings
│   ├── awaiting_reply.py         — snooze escalation ladder
│   ├── message_voice.py / reply_delivery.py — Telegram-independent reply path
│   ├── inbox_feed.py             — app inbox feed
│   ├── google_service.py / outlook_service.py — Calendar/Tasks/Gmail + Outlook APIs
│   ├── push_notification.py      — FCM push
│   ├── llm.py                    — LLM client helpers
│   ├── user_settings.py / seeding.py / example_entities.py
├── prompts/                      — Prompt registry (separate from code)
│   ├── classify.py / email_classify.py / briefing.py / query.py / planner.py
│   ├── workflow.py / relationship.py / voice.py / guards.py
├── retrieval/                    — Hybrid retrieval engine
│   ├── search.py                 — associative_retrieve() multi-signal ranking
│   ├── chunker.py / normalizer.py / schema.py / config.py
│   ├── graph.py / ppr.py / ranking.py / extractor.py
│   ├── pipeline.py               — index_memory(), pending_retrieval_index_jobs
│   ├── backfill.py / cleanup.py  — index backfill + maintenance
│   └── eval.py / seed_eval_gold.py — retrieval evaluation harness
├── lib/                          — Utilities
│   ├── constants.py / time_utils.py / redis_cache.py / rate_limiter.py
│   ├── ingest.py                 — unified ingestion pipeline contract (direction-aware)
│   ├── entity_linker.py / entity_detector.py / people_utils.py — deterministic resolution
│   ├── enrichment_queue.py       — queue-based enrichment (survives cold starts)
│   ├── state_machines.py         — formal state machines
│   ├── url_filter.py / duplicate_guard.py / message_sieve.py / ask_detector.py
│   ├── decision_audit.py / decision_features.py / learning_hints.py / pattern_extractor.py / telemetry.py
│   ├── graph_rules.py / node_tables.py / clarification_state.py
│   ├── conversation.py / chat_split.py / episode_context.py / stream_adapter.py
│   ├── document_extractor.py / planner_critic.py / query_timer.py / rhodey_voice.py
│   └── temporal_lineage.py / audit_logger.py
└── context/                      — Context Registry
    ├── registry.py / strategies.py / pipeline.py
    ├── config.py                 — 6 strategy configs
    ├── gates.py                  — entity-grounding gates
    └── schema.py                 — ContextResult, GateResult types
```

### Key Design Decisions

- **Serverless on Modal**: Python 3.11 FastAPI; long webhook timeout; enrichment queue survives cold starts (built because the old platform killed background tasks)
- **Multi-tenant facade**: `TenantAwareClient` + `tenant_scope` inject `owner_id` on every write/filter on every read; only `_GLOBAL_RPCS` (e.g. `run_sql`) escape scoping
- **Action Planner (unified)**: single typed Action pipeline with typed contracts, PATCH semantics, deterministic scheduling, and a no-retry contract on parallel LLM pipelines
- **Enrichment queue**: `pending_enrichment_jobs` with atomic claim
- **Parallel context assembly**: `asyncio.gather` in briefing.py and dispatch.py
- **Decisions ledger**: every decision persists with `metadata.learn_features` and trains `subsystem_patterns` — undo emits a demoting correction
- **Streaming queries**: user queries stream Gemini responses
- **Direction-awareness**: own-sends (your emails/Teams/Outlook) never surface as inbound items

## Frontend (Next.js — PARKED)

The Next.js dashboard (`frontend/`) exists but is **parked (decision D3, 2026-08-15)** — not part of active gates, not maintained for new features. Stack: Next.js App Router, React, shadcn/ui, Tailwind, PixiJS v8 (NeuralDisc 3D graph), SWR, Supabase SSR. Do not assume its module list is current; verify before touching.

## Mobile App (Flutter — Rhodey) — ACTIVE

The primary surface. `rhodey_app/`:
- **Onboarding** — Google / email-OTP sign-in, persona setup, "how Rhodey works" primer
- **Home modes** — proceed / decide / sprint / catch-up / wrap
- **Inbox** — quick confirmations (type filter, selection-mode batch approve/reject), channel batch approve, **per-item undo**
- **Today / history / entities** — day plan, decision audit, knowledge-graph entities
- **Voice** — capture + rendered acks via a Telegram-independent reply path (`core/services/reply_delivery.py`, `message_voice.py`)
- **Settings** — persona, voice, notification, account

See `rhodey_app/README.md` for the screen map and testing instructions.
