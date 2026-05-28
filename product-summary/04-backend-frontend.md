# 4. Backend & Frontend

## Backend (Python/FastAPI)

### Entry Point
A single serverless function at `api/index.py` handles all HTTP traffic via Vercel `rewrites`. 12 API routes:

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/` | GET | None | Health check |
| `/api/webhook` | POST | Chat ID | Telegram message intake |
| `/api/pulse` | POST | HMAC + Secret | Trigger briefing engine |
| `/api/send-message` | POST | X-API-Key | Web UI message → Telegram processing |
| `/api/send-draft` | POST | X-API-Key | Send email draft |
| `/api/email-action` | POST | X-API-Key | Approve/reject email pending task |
| `/api/messages` | GET | X-API-Key | Message history |
| `/api/calendar-events` | GET | X-API-Key | Google + Outlook calendar events |
| `/api/tasks/{id}/status` | PATCH | X-API-Key | Mark task done (with outcome memory) |
| (4 frontend proxy routes) | Various | X-API-Key | Additional data queries |

### Module Architecture

```
api/index.py                      — FastAPI app, 12 routes, auth middleware
core/
├── webhook/                      — Real-time Telegram processing
│   ├── handler.py                — Main webhook: dedup, auth, multimodal, routing
│   ├── classify.py               — Gemini intent classification, embeddings
│   ├── dispatch.py               — Route by intent, all handlers (813 lines)
│   ├── commands.py               — 18 Telegram command handlers
│   ├── email.py                  — Email pending decisions, draft management
│   ├── multimodal.py             — Image/audio/document processing via Gemini
│   ├── telegram.py               — Telegram API send wrapper
│   └── utils.py                  — Hybrid search, GitHub dispatch trigger
├── pulse/                        — Scheduled intelligence engine
│   ├── engine.py                 — Main briefing cycle (1,757 lines)
│   ├── memory.py                 — Hindsight, serendipity, adaptive learning
│   ├── graph.py                  — Knowledge graph operations
│   ├── calendar.py               — Google Calendar/Tasks sync
│   ├── llm.py                    — LLM calls with fallback chain
│   ├── practices.py              — Practice detection and lifecycle
│   ├── resources.py              — Resource enrichment and mission linking
│   ├── pipeline.py               — Heartbeat, health checks
│   └── utils.py                  — Formatting, routing context
├── agents/                       — Autonomous workers
│   ├── research_agent.py         — Jina AI web search + Gemini dossier
│   ├── quick_process.py          — Inline raw dump processing
│   ├── janitor_check.py          — Pipeline health diagnostics
│   └── cleanup_orphans.py        — Database maintenance
├── skills/                       — Batch scripts (CI-run)
│   ├── archive_ingest.py         — Google Sheets journal → memories
│   ├── backfill_graph.py         — LLM-powered graph edge sync
│   ├── brain_synth.py            — Canonical page consolidation
│   ├── email_ingest.py           — Gmail inbox processing
│   ├── outlook_ingest.py         — Outlook inbox processing
│   └── outlook_token_helper.py   — Outlook OAuth token management
├── services/                     — Shared services
│   ├── db.py                     — Supabase client, versioned_update, embeddings
│   ├── google_service.py         — Google Calendar + Tasks API
│   ├── llm.py                    — Shared LLM calls
│   ├── telegram.py               — Telegram messaging
│   ├── outlook_service.py        — Outlook Calendar API
│   └── pipeline_service.py       — Failed queue management
└── lib/                          — Utilities
    ├── constants.py              — Email status enum
    ├── conversation.py           — Session management, history formatting
    ├── duplicate_guard.py        — Three-tier dedup
    ├── temporal_lineage.py       — Versioning, time-travel, drift detection
    ├── rate_limiter.py           — Sliding window rate limiter
    ├── people_utils.py           — Name normalization, blocklist
    └── audit_logger.py           — Audit trail logging
```

### Key Design Decisions

- **Serverless**: Runs as a single Vercel serverless function with 60s timeout
- **Inline + async hybrid**: Telegram processing happens inline (fast), heavy lifting is background (quick_process, research agent)
- **Fire-and-forget pattern**: Task creation runs inline; the webhook returns immediately while processing continues
- **All rewrites, no routes**: Uses Vercel `rewrites` (not `routes`) to avoid cross-project interference (two Vercel projects share one GitHub repo)

## Frontend (Next.js 16 / React 19)

### Stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js 16 App Router |
| UI | React 19, shadcn/ui, Radix UI |
| Styling | Tailwind v4, OKLCH color tokens |
| Icons | Lucide React |
| Visualization | D3.js (force-directed graph) |
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
| People | Grid with strategic weight, role, source tracking |
| Resources | Library grid, mission groups, category filters |
| Memories | Interactive D3 knowledge graph: EgoGraph + FullGraph + NodeFlyout |
| Health | Pipeline health, failed queue, error logs, memory stats |
| Messages | History with direction, status, source, content |

### Design System

The frontend uses a 607-line design specification (`DESIGN.md`) governing:
- OKLCH color space with muted teal brand accent
- Dark/light mode parity (both designed, not toggled)
- Card-premium utility pattern for consistent surfaces
- Status vocabulary with consistent badge variants across all modules
- Empty states required for every module
- Anti-patterns list: 14 banned patterns

### Deployment

Two Vercel projects share this GitHub repo:
- **integrated-os** (backend): Root `.`, Python FastAPI, `vercel.json` with `rewrites` + `functions`
- **integrated-os-frontend** (frontend): Root `frontend/`, Next.js 16, auto-detected (no `vercel.json`)
