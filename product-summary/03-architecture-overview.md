# 3. Architecture Overview

## Architecture (5 Layers + Infrastructure)

Integrated-OS operates as 5 vertical pipeline layers atop a shared infrastructure layer:

```
┌──────────────────────────────────────────────────────────────┐
│                    INGESTION LAYER                           │
│  Telegram │ WhatsApp │ Email │ Outlook │ Teams │ Calls       │
│  → classify() → url_filter() → plan_actions()               │
│  Unified ingest() contract for all channels                 │
├──────────────────────────────────────────────────────────────┤
│                    PROCESSING LAYER                          │
│  Action Planner → Executor → create_*_direct / update_*     │
│  Entity linker (resolve BEFORE creation)                    │
│  Enrichment queue (pending_enrichment_jobs)                 │
│  DLQ consumer │ State machine guards │ Compensate on fail   │
├──────────────────────────────────────────────────────────────┤
│                    INTELLIGENCE LAYER                        │
│  Associative retrieval (7 signals + PPR)                   │
│  Knowledge graph (HITL for all edges)                       │
│  Context registry (6 strategies, entity-grounded)           │
│  Brain synthesis / Pattern detection / Memory clustering    │
├──────────────────────────────────────────────────────────────┤
│                    PRESENTATION LAYER                        │
│  Pulse Engine (single LLM call, write-behind)              │
│  Decision Pulse (AI-free, pending approvals)                │
│  Sentinel (meeting alarms + piggybacks)                     │
│  Health monitor (consolidated)                              │
├──────────────────────────────────────────────────────────────┤
│                    SURFACE LAYER                             │
│  Telegram bot (active, planning deprecation)                │
│  Web UI Dashboard (Next.js 16 / React 19)                   │
│  Flutter Mobile App (Rhodey)                                │
└──────────────────────────────────────────────────────────────┘

╔══════════════════════════════════════════════════════════════╗
║              INFRASTRUCTURE (Cross-Cutting)                   ║
║  Database (Supabase/PostgREST) │ Google Calendar/Tasks       ║
║  Gmail/Outlook │ Telegram │ FCM Push                         ║
║  GitHub Actions │ cron-job.org │ Vercel                      ║
║  Upstash Redis (cache)                                      ║
╚══════════════════════════════════════════════════════════════╝
```

## System Components

### API Layer (Vercel Serverless Function)
A single Python FastAPI application (`api/index.py`) handles all HTTP traffic. Routes serve Telegram webhooks, the Pulse briefing engine, frontend API proxying, health checks, and diagnostic endpoints. All routes are rewritten to this single function via `vercel.json` `rewrites`. The webhook is wrapped in `asyncio.wait_for(timeout=55)` to intercept Vercel's 60s hard kill and return cleanly with a "still thinking" placeholder.

### Webhook Handler (`core/webhook/`)
The primary entry point for real-time data. Processes Telegram updates through a pipeline: dedup → auth → multimodal dispatch → shortcode resolution → clarification handling → intent classification → routing. URL quarantine at ingress (`url_filter.py`) routes bare URLs directly to resources table with no LLM call.

### Action Pipeline (`core/actions/`)
Replaced the legacy 3-headed architecture (Webhook + Quick Process cron + Pulse Engine staging sorter) with a single typed Action pipeline:
- **`planner.py`**: Single LLM call resolves user intent into typed `Action` objects (create_task, close_task, reschedule, cancel_recurring, etc.) using a multi-source candidate pool (active tasks + recurring tasks + 14-day calendar window)
- **`executor.py`**: Executes actions through `create_task_direct()` / `create_note_direct()` / `update_task_status()` — direct DB operations, no legacy piping
- **`models.py`**: Typed `Action` and `Operation` dataclasses

### Pulse Engine (`core/pulse/`)
A scheduled intelligence cycle using a **single LLM call** (no agent loop) with write-behind pattern. Formerly a 1500-line `engine.py` with agent loop + staging sorter, now 6 focused modules:
- `briefing.py` — Single LLM call, parallel context assembly (Phase 1 + Phase 2 via `asyncio.gather`)
- `decision_pulse.py` — AI-free pending approvals
- `sentinel.py` — Meeting alarms + 7 piggyback maintenance jobs
- `pipeline.py` — Consolidated health monitor
- `models.py` — Clean data contracts
- `run_logger.py` — Pulse run tracking

### Skills (`core/skills/`)
Standalone batch scripts run via GitHub Actions CI: archive/journal ingest, email ingest (Gmail + Outlook), graph backfill, brain synthesis, DLQ consumer.

### Agents (`core/agents/`)
Autonomous workers: research agent (Jina AI web search), actions directory (Action Planner). Legacy agents (janitor, cleanup, quick process) all removed — their functions absorbed by the consolidated health monitor, enrichment queue, and sentinel piggybacks.

### Frontend (`frontend/`)
Next.js 16 / React 19 dashboard with 10+ modules, Supabase auth, PixiJS v8 NeuralDisc (3D spherical graph visualization — replaced legacy D3.js), shadcn/ui design system, and OKLCH color tokens.

### Mobile (Flutter)
Full Rhodey app (`rhodey_app/`) with 12 screens, 5 models, 3 services, 4 widgets. Firebase integration with FCM push notifications. In-app update system with version check/download/install. TTS for Rhodey responses. Voice mic button on home screen. Horizon/Traces home screen design with warm stone palette.

## Data Flow (End to End)

```
Telegram Message / Web UI / Flutter App
    → api/index.py (FastAPI proxy)
    → url_filter.py (URL quarantine at ingress)
    → classifier (Gemini Flash Lite — intent + entity)
    → Route by Intent:
        TASK/COMPLETION/NOTE/PROJECT_UPDATE → plan_actions()
            → execute_planned_actions()
            → create_task_direct / create_note_direct (with entity resolution BEFORE creation)
            → enrichment_queue (graph edges, entities, embeddings — survives Vercel cold kills)
            → Google Calendar sync + Google Tasks sync
        QUERY → interrogate_brain()
            → Anaphora resolution (resolve pronouns via active_anchor)
            → Parallel context fetch (associative retrieval, graph, calendar, memories, emails, etc.)
            → Gemini reasoning (streaming)
        DAILY_BRIEF → handle_daily_brief()
        CLARIFICATION_NEEDED → handle_clarification()
        NOISE → silent ack
    → Telegram/FCM push/Task response

Scheduled Pulse (via GitHub Actions/cron-job.org)
    → Briefing (3-7x daily): build context → single LLM call → Telegram
    → Decision Pulse (every 30min): pending approvals → inline keyboard → Telegram
    → Sentinel (every 5min): check upcoming events → nudge → piggyback maintenance
    → Health monitor (every 2h): DLQ, error logs, LLM degradation, orphan sweep
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.12 (backend), Node.js (frontend) |
| Backend Framework | FastAPI |
| Frontend Framework | Next.js 16 App Router |
| Mobile | Flutter (Dart) |
| Database | Supabase (PostgreSQL + pgvector) |
| LLM | Gemini 3.5 Flash (synthesis), Gemini 3.1 Flash Lite (classification), Gemini Embedding 2 |
| LLM Fallback | Gemma 4, OpenRouter |
| Search | Jina AI (web search for research agent) |
| Distributed Cache | Upstash Redis (REST API) |
| Calendar | Google Calendar API, Microsoft Graph API (Outlook) |
| Tasks | Google Tasks API |
| Email | Gmail API, Microsoft Graph API (Outlook) |
| Messaging | Telegram Bot API |
| Push | Firebase Cloud Messaging (FCM) |
| Auth | HMAC-SHA256 + API Key + PULSE_SECRET + Supabase Service Role |
| CI/CD | GitHub Actions (8+ workflows) + cron-job.org |
| Hosting | Vercel (serverless functions + static export) |
| Document Extraction | PyMuPDF (PDF), python-docx (DOCX), openpyxl (XLSX), python-pptx (PPTX) |
| Data Visualization | PixiJS v8 WebGL (NeuralDisc), D3.js (graph) |
| UI Framework | shadcn/ui + Radix UI + Tailwind v4 |
