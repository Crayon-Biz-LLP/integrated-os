# 3. Architecture Overview

> Updated 2026-08-15. The older platform / Python-version / Telegram-deprecation /
> organization-level-brain claims below have been corrected — see
> `99-architecture-reference.md` for the definitive reference.

## Architecture (5 Layers + Infrastructure)

```
┌──────────────────────────────────────────────────────────────┐
│                    INGESTION LAYER                           │
│  Telegram │ WhatsApp (Beeper) │ Email │ Outlook │ Teams      │
│  → classify() → url_filter() → plan_actions()               │
│  Unified ingest() contract — direction-aware (own-sends      │
│  never surface)                                             │
├──────────────────────────────────────────────────────────────┤
│                    PROCESSING LAYER                          │
│  Action Planner → Executor → create_*_direct / update_*     │
│  Entity linker (resolve BEFORE creation)                    │
│  Enrichment queue (pending_enrichment_jobs)                 │
│  DLQ consumer │ Typed contracts │ Compensate on fail        │
├──────────────────────────────────────────────────────────────┤
│                    INTELLIGENCE LAYER                        │
│  Hybrid retrieval (vector + graph, multi-signal)            │
│  Knowledge graph (HITL for all edges, per-item undo)        │
│  Context registry (6 strategies, entity-grounded)           │
│  Decisions ledger + subsystem learning loop                 │
├──────────────────────────────────────────────────────────────┤
│                    PRESENTATION LAYER                        │
│  Pulse (single LLM call, write-behind)                     │
│  Decision Pulse (AI-free, pending approvals)                │
│  Sentinel (meeting alarms + piggybacks)                     │
│  Health monitor (consolidated) + app_intelligence (Intel)   │
├──────────────────────────────────────────────────────────────┤
│                    SURFACE LAYER                             │
│  Telegram (primary) │ Email │ Teams │ Beeper/WhatsApp        │
│  Flutter app (Rhodey) — onboarding, personas, home modes,    │
│  voice, per-item undo, Telegram-independent reply path       │
│  Next.js dashboard (parked)                                 │
└──────────────────────────────────────────────────────────────┘

╔══════════════════════════════════════════════════════════════╗
║              INFRASTRUCTURE (Cross-Cutting)                   ║
║  Modal (deploy) │ Supabase/PostgREST │ Upstash Redis         ║
║  Google Calendar/Tasks/Gmail │ Microsoft Graph (Outlook)     ║
║  Telegram Bot API │ FCM Push │ GitHub Actions (22 workflows) ║
╚══════════════════════════════════════════════════════════════╝
```

## Multi-Tenant Layer (M3, cross-cutting)

Since M3 (Aug 2026) every tenant is isolated end-to-end:

- **`core/services/db.py`** — `TenantAwareClient` + `tenant_table()` inject the
  caller's `owner_id` on every write and filter on every read; `tenant_scope`
  context manager sets the ambient tenant; `_GLOBAL_RPCS` (e.g. `run_sql`) are
  the only non-scoped surface.
- **Sign-in** — Google / email-OTP (`core/services/auth.py`, `otp_email.py`)
  replaced the API-key paste; per-tenant **LLM spend caps** (`llm_spend`,
  `users.monthly_credit_usd`).
- **DB grants** — reworked in db/87–91: anon revoked, per-tenant roles,
  owner-scoped RPC matrix (verified by `tests/tenants/test_db_isolation.py`).

## System Components

### API Layer (Modal)
A single Python FastAPI application (`api/index.py`, deployed via
`infra/modal_app.py`) handles all HTTP traffic on Modal (Python 3.11). Routes
serve Telegram webhooks, the Pulse briefing engine, `/api/` endpoints for the
app (auto-decisions, confirm/reject/undo, pulse-cron), health checks, and
diagnostics.

### Webhook Handler (`core/webhook/`)
Primary entry point for real-time data. Pipeline: dedup → **auth**
(incoming chat must match the bound tenant chat; `TEST_CHAT_IDS` allow-list for
the test harness, default-off) → multimodal dispatch → shortcode resolution →
clarification handling → intent classification → routing. URL quarantine at
ingress (`url_filter.py`). `email.py` handles the email/Teams/Outlook side.

### Action Pipeline (`core/actions/`)
Replaced the legacy 3-headed architecture with a single typed Action pipeline:
- **`planner.py`** — single LLM call resolves intent into typed `Action`
  objects using a multi-source candidate pool
- **`executor.py`** — typed contracts, PATCH semantics, deterministic
  scheduling, parallel LLM pipelines with a no-retry contract
- **`models.py`** — typed `Action` / `Operation` dataclasses

### Pulse (`core/pulse/`)
Scheduled intelligence using a **single LLM call** with write-behind:
- `briefing.py` — single LLM call, parallel context assembly, direction-aware
- `decision_pulse.py` — AI-free pending approvals
- `sentinel.py` — meeting alarms + piggyback maintenance jobs
- `pipeline.py` — consolidated health monitor
- `graph.py` — knowledge-graph decisions (node/edge approve/reject) with
  decision-time learn features persisted via `core/decisions.py`
- `run_logger.py` — pulse run tracking

### Decisions & Learning Loop (`core/decisions.py`)
Every user decision — approve / reject / snooze / confirm / undo — persists to
the `decisions` table with `metadata.learn_features` (the exact decision-time
feature dict) and trains `subsystem_patterns` / `subsystem_telemetry` per
subsystem. Undo emits an `undo_correction` that demotes the pattern. This is
the product's core promise: "Not now" trains, never resets.

### Skills, Agents & Services
- **Skills** (`core/skills/`) — standalone batch scripts (archive/journal
  ingest, email ingest, graph backfill, brain synthesis, DLQ consumer).
- **Agents** (`core/agents/`) — research agent (web search → dossier); legacy
  janitor/cleanup/quick-process agents removed.
- **Services** (`core/services/`) — `auth`, `persona`, `onboarding`,
  `briefing_refresh` (silent push briefings), `awaiting_reply` (snooze
  escalation ladder), `message_voice`, `outlook_service`, `google_service`,
  `push_notification`, `inbox_feed`, `llm`, `db`.

### Frontend (`frontend/`)
Next.js / React dashboard — **parked (D3)**. Not part of active gates.

### Mobile — Rhodey (`rhodey_app/`)
Flutter app: onboarding (Google / email-OTP sign-in), persona layer, home-screen
modes (proceed/decide/sprint/catch-up/wrap), inbox with quick confirmations +
selection-mode batch approve/reject + **per-item undo**, today/history/entities
screens, voice capture with a Telegram-independent reply path, FCM push, and an
in-app update system.

## Data Flow (End to End)

```
Telegram / Beeper / Email / Teams / Flutter App
    → api/index.py (FastAPI proxy)
    → url_filter.py (URL quarantine at ingress)
    → classifier (Gemini Flash Lite — intent + entity)
    → Route by Intent:
        TASK/COMPLETION/NOTE → plan_actions()
            → execute_planned_actions() (typed contracts)
            → create_task_direct / create_note_direct (entity resolution BEFORE creation)
            → enrichment_queue (graph edges, entities, embeddings)
            → Google Calendar sync + Google Tasks sync
            → decision recorded + subsystem pattern trained
        QUERY → interrogate_brain()
            → Anaphora resolution (resolve pronouns via conversation_threads.active_anchor)
            → Parallel context fetch (associative retrieval, graph, calendar, memories, emails)
            → Gemini reasoning (streaming)
        DAILY_BRIEF → handle_daily_brief()
        CLARIFICATION_NEEDED → handle_clarification()
        NOISE → silent ack
    → Telegram / FCM push / in-app response

Scheduled Pulse (Modal cron + cron-job.org)
    → Briefing (3-7x daily): build context → single LLM call → Telegram/app
    → Decision Pulse (every 30min): pending approvals → inline keyboard
    → Sentinel (every 5min): upcoming events → nudge → piggyback maintenance
    → Health monitor (every 2h): DLQ, error logs, LLM degradation, orphan sweep
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11 (backend), Dart/Flutter (mobile), Node.js (frontend, parked) |
| Backend Framework | FastAPI |
| Mobile | Flutter (Rhodey) |
| Database | Supabase (PostgreSQL + pgvector) |
| LLM | Gemini 3.6 Flash (synthesis), Gemini 3.5 Flash Lite (classification) |
| Search | Jina AI (web search for research agent) |
| Distributed Cache | Upstash Redis (rate limiter, test sandbox lock) |
| Calendar | Google Calendar API, Microsoft Graph API (Outlook) |
| Tasks | Google Tasks API |
| Email | Gmail API, Microsoft Graph API (Outlook) |
| Messaging | Telegram Bot API, Beeper bridge (WhatsApp), Teams |
| Push | Firebase Cloud Messaging (FCM) |
| Auth | Google / email-OTP sign-in + service role + PULSE_SECRET (cron) |
| CI/CD | GitHub Actions (22 workflows) + cron-job.org; test gates (fast/nightly) |
| Hosting | Modal (serverless functions) |
| Document Extraction | PyMuPDF (PDF), python-docx (DOCX), openpyxl (XLSX), python-pptx (PPTX) |
