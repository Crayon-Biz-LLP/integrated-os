# 4. Backend & Frontend

## Backend (Python/FastAPI)

### Entry Point
A single serverless function at `api/index.py` handles all HTTP traffic via Vercel `rewrites`. Routes serve Telegram webhooks, the Pulse briefing engine, frontend API proxying, health checks, diagnostic endpoints, web chat, and deploy hooks.

### Module Architecture

```
api/index.py                      — FastAPI app, 20+ routes, auth middleware
core/
├── webhook/                      — Real-time Telegram processing
│   ├── handler.py                — Main webhook: dedup, auth, multimodal, routing
│   ├── classify.py               — Gemini intent classification
│   ├── dispatch.py               — Route by intent, interrogate_brain, all handlers
│   ├── commands.py               — Telegram command handlers
│   ├── multimodal.py             — Image/audio/document processing (uses hybrid extractor)
│   ├── telegram.py               — Telegram API send wrapper + FCM push trigger
│   ├── workflows.py              — Workflow state engine (batch enrichment)
│   ├── why_handler.py            — /why decision audit handler
│   └── utils.py                  — Pending decision processor, hybrid search helpers
├── actions/                      — Unified Action Planner
│   ├── planner.py                — LLM resolves intent into typed Action objects
│   ├── executor.py               — Execute actions with validation + compensation
│   └── models.py                 — Action/Operation dataclasses
├── pulse/                        — Scheduled intelligence engine
│   ├── briefing.py               — Single LLM call, parallel context assembly, write-behind
│   ├── decision_pulse.py         — AI-free pending approvals
│   ├── sentinel.py               — Meeting alarms + 7 piggyback maintenance jobs
│   ├── engine.py                 — Legacy (deprecated — replaced by briefing.py)
│   ├── memory.py                 — Hindsight, serendipity, adaptive learning
│   ├── graph.py                  — Knowledge graph operations, entity resolution
│   ├── calendar.py               — Google Calendar/Tasks sync
│   ├── tools.py                  — create_task_direct, create_note_direct, update_task_status
│   ├── llm.py                    — Legacy LLM helpers (ToolRegistry was here, now removed)
│   ├── practices.py              — Practice detection and lifecycle
│   ├── resources.py              — Resource enrichment and cluster linking
│   ├── pipeline.py               — Consolidated health monitor (DLQ + errors + LLM degradation)
│   ├── models.py                 — PulseOutput, BriefingContext data contracts
│   ├── context.py                — ContextProvider: 6 TTL caches, hydrate_* functions
│   ├── run_logger.py             — Pulse run tracking
│   └── utils.py                  — Formatting, routing context
├── agents/                       — Autonomous workers
│   ├── research_agent.py         — Jina AI web search + Gemini dossier
│   └── quick_process.py          — (DELETED — replaced by Action Planner)
├── skills/                       — Batch scripts (CI-run)
│   ├── archive_ingest.py         — Google Sheets journal → memories
│   ├── backfill_graph.py         — LLM-powered graph edge sync
│   ├── brain_synth_v2.py         — Canonical page consolidation
│   ├── email_ingest.py           — Gmail inbox processing
│   ├── outlook_ingest.py         — Outlook inbox processing
│   ├── call_ingest.py            — Call recording transcription + extraction
│   ├── whatsapp_ingest.py        — WhatsApp notification ingest + batch RPC
│   ├── dlq_consumer.py           — Dead letter queue consumer
│   └── teams_ingest.py           — Microsoft Teams ingest
├── services/                     — Shared services
│   ├── db.py                     — Supabase client, get_supabase(), get_embedding()
│   ├── google_service.py         — Google Calendar/Tasks/Gmail API (build() LRU-cached)
│   ├── push_notification.py      — FCM push notification service
│   └── outlook_service.py        — Outlook Calendar API
├── prompts/                      — Prompt registry (all prompts separated from inline code)
│   ├── classify.py               — Intent classification prompt
│   ├── briefing.py               — Pulse briefing + daily brief prompts
│   ├── query.py                  — Interrogate brain prompt (streaming + non-streaming)
│   ├── planner.py                — Action Planner operation resolution prompt
│   ├── workflow.py               — Workflow resume + enrichment prompts
│   ├── voice.py                  — Rhodey's character bible (tone, voice)
│   ├── guards.py                 — Action guards (no hallucination, no self-narration)
│   └── ingest.py                 — (DELETED — replaced by workflow.py)
├── retrieval/                    — Associative retrieval engine
│   ├── search.py                 — associative_retrieve() — 7-signal ranking
│   ├── pipeline.py               — index_memory(), schedule_index_memory(), pending_retrieval_index_jobs
│   ├── graph.py                  — Phrase node graph operations, build_triple_graph()
│   ├── ranking.py                — 7-signal ranking weights and blending
│   ├── ppr.py                    — personalized_pagerank() — bounded subgraph
│   ├── extractor.py              — LLM entity extraction from memory text
│   ├── config.py                 — Per-site feature flags
│   └── eval.py                   — Side-by-side comparison of legacy vs associative
├── lib/                          — Utilities
│   ├── conversation.py           — Session management, history formatting, classify context
│   ├── entity_linker.py          — Deterministic entity resolution (org/project/people)
│   ├── enrichment_queue.py       — Queue-based enrichment (survives Vercel cold kills)
│   ├── state_machines.py         — Formal state machines for 16 tables
│   ├── url_filter.py             — URL quarantine single source of truth
│   ├── node_tables.py            — pending_nodes / merge_proposals abstraction
│   ├── clarification_state.py    — DB-backed clarification state
│   ├── ingest.py                 — Unified ingestion pipeline contract
│   ├── document_extractor.py     — Hybrid document extraction (PyMuPDF, docx, xlsx, pptx)
│   ├── time_utils.py             — now_ist(), IST_TIMEZONE, format_rfc3339
│   ├── decision_audit.py         — ReasonCode enum, log_decision(), decision_chain_id
│   ├── redis_cache.py            — Upstash Redis cache (get/set/delete)
│   ├── rate_limiter.py           — asyncio.Lock + Redis-backed rate limiting
│   ├── audit_logger.py           — Structured audit trail logging
│   ├── temporal_lineage.py       — Versioning, time-travel, drift detection
│   └── duplicates.py             — Semantic dedup utilities
└── context/                      — Context Registry
    ├── pipeline.py               — Strategy-based context hydration
    ├── config.py                 — 6 strategy configs
    ├── gates.py                  — Entity-grounding gates (hard/soft/none)
    └── schema.py                 — ContextResult, GateResult types
```

### Key Design Decisions

- **Serverless**: Runs as a single Vercel serverless function with 60s timeout; wrapped in `asyncio.wait_for(55)` for clean timeout recovery
- **Action Planner (unified)**: Single typed Action pipeline replaces legacy 3-headed architecture (Webhook + Quick Process cron + Pulse Engine sorter)
- **Enrichment queue**: `pending_enrichment_jobs` with atomic claim → survives Vercel cold kills (replaced fire-and-forget `asyncio.create_task`)
- **Parallel context assembly**: `asyncio.gather` in briefing.py and dispatch.py for independent DB/LLM queries
- **Streaming queries**: User queries use streaming Gemini responses for faster time-to-first-token
- **All rewrites, no routes**: Uses Vercel `rewrites` (not `routes`) to avoid cross-project interference (two Vercel projects share one GitHub repo)

## Frontend (Next.js 16 / React 19)

### Stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js 16 App Router |
| UI | React 19, shadcn/ui, Radix UI |
| Styling | Tailwind v4, OKLCH color tokens |
| Icons | Lucide React |
| Visualization | PixiJS v8 (NeuralDisc 3D graph), D3.js |
| Auth | Supabase SSR |
| Data Fetching | SWR (30s auto-refresh) |
| Toasts | Sonner |
| Theme | next-themes (light + dark parity) |

### Dashboard Modules

| Module | What It Shows |
|--------|-------------|
| Home | Stats cards, WhatToDoNow, QuickChat, Pulse Briefings, Recent Tasks |
| Tasks | Filterable table with priorities, project context, detail sheet |
| Projects | Grid cards with org tags, status, keyword context |
| Emails | Inbox table, draft list, pending tasks, classification filters |
| Calendar | Month/week/day/agenda views, Google + Outlook unified |
| Graph | Split-pane: Episode Stream (left) + NeuralDisc 3D (right) |
| People | Grid with strategic weight, role, source tracking |
| Resources | Library grid, cluster groups, category filters, list/grid toggle |
| Decisions | Pending graph edges with Approve/Edit/Reject, badge count |
| Health | Pipeline health, DLQ, error logs, memory stats |
| Messages | History with direction, status, source, content |
| Habits | Weekly habit grid with completion tracking |

### Design System

The frontend uses OKLCH color space with muted teal brand accent, dark/light mode parity, card-premium utility pattern, and consistent status badge variants across all modules.

### Deployment

Two Vercel projects share this GitHub repo:
- **integrated-os** (backend): Root `.`, Python FastAPI, `vercel.json` with `rewrites` + `functions`
- **integrated-os-frontend** (frontend): Root `frontend/`, Next.js 16, auto-detected (no `vercel.json`)

## Mobile App (Flutter — Rhodey)

Full Flutter app with 12 screens, Firebase integration (FCM push), in-app update system, TTS for Rhodey responses, voice mic button, and Horizon/Traces home screen design.
