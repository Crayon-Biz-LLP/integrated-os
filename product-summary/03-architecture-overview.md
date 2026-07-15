# 3. Architecture Overview

## The Triangular Engine

Integrated-OS operates as three interconnected layers:

```
┌─────────────────────────────────────────────────────────┐
│                     INTAKE                               │
│  Telegram Webhook │ Gmail/Outlook │ Google Forms/Journal │
│  Text / Voice / Images / PDFs / Emails / Journal Entries │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                   INTELLIGENCE                           │
│  Intent Classification (Gemini)  │  Memory (Vector DB)   │
│  Entity Routing (Stealth)        │  Knowledge Graph      │
│  Hybrid Search (Vector + Graph)  │  Serendipity Engine   │
│  Canonical Pages (Brain Synth)   │  Practice Detection   │
│  Adaptive Briefing Learner       │  Temporal Patterns    │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     EXECUTION                            │
│  Task Creation │ Google Calendar │ Google Tasks          │
│  Project/People Auto-Creation   │ Graph Edge Creation    │
│  Telegram Briefings (The Pulse) │ Email Draft Sending   │
│  After-Action Reports           │ Season Governance      │
└─────────────────────────────────────────────────────────┘
```

## System Components

### API Layer (Vercel Serverless Function)
A single Python FastAPI application (`api/index.py`, 312 lines) handles all HTTP traffic. 12 routes serve Telegram webhooks, the Pulse briefing engine, frontend API proxying, and health checks. All routes are rewritten to this single function via `vercel.json` `rewrites`.

### Webhook Handler (`core/webhook/`)
The primary entry point for real-time data. Processes Telegram updates through a 6-stage pipeline: dedup → auth → multimodal dispatch → shortcode resolution → clarification handling → intent classification → routing.

### Pulse Engine (`core/pulse/`)
The brain of the system. A scheduled intelligence cycle that utilizes a **Context Hydration Engine** to assemble vector/graph context, and a **ToolRegistry** powered agent-loop to process tasks, create entities, and write memories. It runs 5 parallel AI agents, generates a structured briefing, syncs with Google, and delivers the briefing via Telegram.

### Skills (`core/skills/`)
Standalone batch scripts run via GitHub Actions CI: archive/journal ingest, email ingest (Gmail + Outlook), graph backfill, brain synthesis.

### Agents (`core/agents/`)
Autonomous workers: research agent (Jina AI web search), janitor (pipeline health + LLM degradation detection), cleanup (orphan records), quick process (inline raw dump processing).

### Frontend (`frontend/`)
Next.js 16 / React 19 dashboard with 10 modules, Supabase auth, D3 knowledge graph visualization, and a premium OKLCH design system.

## Data Flow (End to End)

```
Telegram Message
    → Webhook (dedup + auth)
    → Gemini Classification (intent + entity)
    → Route by Intent:
        TASK    → handler.py → plan_actions() → create_task_direct → tasks table + Google Calendar + Google Tasks + graph edges
        NOTE    → raw_dumps → memories table (with embedding)
        QUERY   → hybrid search (graph + vector + canonical) → Gemini answer
        NOISE   → silent ack (👍)
        DELEGATE → agent_queue → research agent → Jina search → Gemini dossier → raw_dumps
    → Telegram acknowledgment

Scheduled Pulse (via GitHub Actions)
    → archive_ingest (Google Sheets journal → memories)
    → backfill_graph (memories → graph edges)
    → pulse_cli.py → engine.py:
        → Zombie recovery
        → Google→Supabase sync
        → Fetch raw_dumps + tasks + projects + people + clusters
        → Staging area sorter (classify dumps into TASK/NOTE/COMPLETION/NOISE)
        → Build context (memories, graph, calendar, hindsight)
        → 5 parallel AI agents
        → Gemini briefing generation (250-line prompt)
        → Write phase (projects, people, tasks, completions, clusters)
        → Resource cluster backfill
        → Briefing formatting + Telegram delivery
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11 (backend), Node.js (frontend) |
| Framework | FastAPI (backend), Next.js 16 App Router (frontend) |
| Database | Supabase (PostgreSQL + pgvector) |
| LLM | Gemini 3 Flash, Gemini 3.1 Flash Lite, Gemini Embedding 2 |
| LLM Fallback | Gemma 4, OpenRouter |
| Search | Jina AI (web search for research agent) |
| Distributed Cache | Upstash Redis (REST API — no persistent connections) |
| Calendar | Google Calendar API, Microsoft Graph API (Outlook) |
| Tasks | Google Tasks API |
| Email | Gmail API, Microsoft Graph API (Outlook) |
| Messaging | Telegram Bot API |
| Auth | HMAC-SHA256 + API Key + Supabase Service Role |
| CI/CD | GitHub Actions (7 workflows, cron scheduled) |
| Hosting | Vercel (serverless functions + static export) |
| Data Visualization | D3.js (force-directed graph) |
| UI Framework | shadcn/ui + Radix UI + Tailwind v4 |
