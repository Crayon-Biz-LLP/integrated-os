# Rhodey OS — 5-Layer Architecture + Infrastructure

```mermaid
%%{init: {'theme': 'neutral', 'themeVariables': { 'primaryColor': '#1a1a2e', 'primaryTextColor': '#e0e0e0', 'primaryBorderColor': '#4a4a6a', 'lineColor': '#6b6b8a', 'secondaryColor': '#16213e', 'tertiaryColor': '#0f3460' }}}%%

flowchart TB
    subgraph S0["Surface Layer"]
        direction LR
        S1["🟡 Telegram Bot\n(active use, planning deprecation)\nPrimary input today\nInline keyboards + notifications\n→ Deprecate to alert-only"]
        S2["🟢 Web UI (Next.js)\nDashboard, Tasks, Emails,\nGraph, Calendar, Clusters"]
        S3["🟢 Flutter App (Rhodey)\nHome feed, Today, Inbox,\nTalk/Voice, Push notifications"]
    end

    subgraph GW["API Gateway"]
        G1["api/index.py (FastAPI)\nProxy routes → Surface ↔ Pipeline"]
    end

    S0 -->|REST API| GW

    subgraph L1["Layer 1 — Ingestion"]
        I1["Channels\nTelegram / WhatsApp / Email\nTeams / Calls / Outlook"]
        I2["classify.py\nGemini Flash Lite\n11 intent types"]
        I3["url_filter.py\nURL quarantine at ingress"]
        I4["ingest()\nUnified contract"]
        I5["Dedup\nupdate_id / dedup_key / DB"]
        I1 --> I2 --> I3 --> I4 --> I5
    end

    subgraph L2["Layer 2 — Processing"]
        P1["plan_actions()\nAction Planner\nLLM operation matching"]
        P2["execute_planned()\nExecutor\n7 operation types"]
        P3["Entity Linker\nresolve org/project\nBEFORE create"]
        P4["Enrichment Queue\npending_enrichment_jobs\natomic claim / retry / DLQ"]
        P5["DLQ Consumer\n3-retry dead letter\nescalation"]
        P6["State Guards\n16 state machines\nvalid transitions only"]
        P1 --> P2
        P2 --> P3
        P2 -.-> P4
        P2 -.-> P5
        P2 --> P6
    end

    subgraph L3["Layer 3 — Intelligence"]
        S1["Associative Retrieval\n7-signal ranking + PPR\nRedis cached entities"]
        S2["Knowledge Graph\n5 node types / 16 edge types\nAll edges via HITL"]
        S3["Context Registry\n6 strategies + semantic gates\nPrevents hallucination"]
        S4["Brain Synthesis\nOrg-level canonical pages\nFor briefings"]
        S5["Pattern Detection\nMemory clustering\nTemporal patterns"]
        S1 --> S2
        S1 -.-> S3
        S2 -.-> S4
        S3 --> S4
    end

    subgraph L4["Layer 4 — Presentation"]
        R1["Pulse Engine\nSingle LLM call\nWrite-behind pattern"]
        R2["Decision Pulse\nAI-free approvals\nPattern matching only"]
        R3["Sentinel\nMeeting alarms\n+ piggyback jobs"]
        R4["Health Monitor\nrun_full_health_check()\nSingle source of truth"]
    end

    GW --> L1
    L1 -->|classified intent| L2
    L2 -->|memory index| L3
    L3 -->|strategic context| L4
    L4 -.->|briefing output| GW

    subgraph INFRA["Infrastructure (Cross-Cutting)"]
        X1["Database\nSupabase / PostgREST\nState machines / versioning"]
        X2["Google APIs\nCalendar / Tasks / Gmail\n404 auto-heal sync"]
        X3["Notifications\nFCM push / Telegram bot"]
        X4["CI/CD\nGitHub Actions / cron-job.org\nPulse / Health / Backfill"]
    end

    L1 -.->|persist| INFRA
    L2 -.->|persist + sync| INFRA
    L3 -.->|persist + retrieve| INFRA
    L4 -.->|dispatch| INFRA
```

## Key Design Decisions

| Decision | Rationale |
|---|---|
| **Single Action Planner path** | No legacy dispatch / quick_process / staging sorter — one code path for all task/note operations |
| **Enrichment queue (not fire-and-forget)** | `pending_enrichment_jobs` with atomic claim — survives Vercel cold kills |
| **All graph edges through HITL** | `pending_graph_edges` approval table — no silent edge creation |
| **Pulse Engine: single LLM call** | No agent loop, write-behind pattern — eliminates runaway loops |
| **Entity resolution BEFORE creation** | Deterministic linker resolves org/project before DB write, not post-hoc |
| **Formal state machines** | 16 tables with documented valid transitions — guards on all status changes |
| **Surface deprecation path** | Telegram → alert-only. Web UI + Flutter app become full interaction surfaces |

## Architecture Data Flow

```
User (via Telegram / Web UI / Flutter App)
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│                       SURFACE LAYER                               │
│  Telegram (deprecating) · Web UI · Flutter App                    │
│  All surface → REST API → api/index.py (FastAPI)                  │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│                         PIPELINE LAYERS                            │
│                                                                   │
│  ┌────────────┐   ┌────────────┐   ┌──────────────┐   ┌────────┐ │
│  │ INGESTION  │──▶│ PROCESSING │──▶│ INTELLIGENCE │──▶│PRESENT │ │
│  │ (Layer 1)  │   │ (Layer 2)  │   │ (Layer 3)    │   │(Layer4)│ │
│  └────────────┘   └────────────┘   └──────────────┘   └────────┘ │
│                                                                   │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │    INFRASTRUCTURE    │
              │  DB · Google · Push  │
              │  GitHub Actions      │
              └──────────────────────┘
```

## Purpose of Each Layer

| Layer | What It Does | What It Does NOT Do |
|---|---|---|
| **Surface** | Receives input, renders output. Telegram, Web UI, Flutter App. | Does not process, classify, or persist. Presentation only. |
| **Ingestion** | Classifies intent, quarantines URLs, unifies channels into `ingest()` contract. | Does not execute actions. Does not enrich. |
| **Processing** | Plans actions (LLM), executes operations, enriches asynchronously, compensates on failure. | Does not retrieve historical data. Does not generate briefings. |
| **Intelligence** | Retrieves memories, manages graph, resolves entities, detects patterns, synthesizes brain pages. | Does not create tasks. Does not process raw input. |
| **Presentation** | Generates briefings (LLM), surfaces pending decisions, monitors health, sends alerts. | Does not create tasks. Does not modify data. Read-only output layer. |
| **Infrastructure** | Database, external APIs (Google), push notifications, CI/CD pipelines. Used by ALL 5 layers above. | Does not contain business logic. Not a pipeline stage — it's the foundation everything runs on. |

## File Map

| Layer | Key Files |
|---|---|
| **Surface** | `frontend/` (Next.js), `rhodey_app/` (Flutter), `api/briefing.py`, `api/index.py` (proxy routes) |
| **Ingestion** | `core/webhook/handler.py`, `core/webhook/dispatch.py`, `core/webhook/classify.py`, `core/lib/url_filter.py`, `core/lib/ingest.py`, `core/prompts/classify.py`, `core/skills/whatsapp_ingest.py`, `core/skills/email_ingest.py`, `core/skills/call_ingest.py`, `core/skills/teams_ingest.py` |
| **Processing** | `core/actions/planner.py`, `core/actions/executor.py`, `core/actions/models.py`, `core/pulse/tools.py`, `core/lib/entity_linker.py`, `core/lib/enrichment_queue.py`, `core/lib/state_machines.py`, `core/skills/dlq_consumer.py`, `core/prompts/planner.py` |
| **Intelligence** | `core/retrieval/search.py`, `core/retrieval/ranking.py`, `core/retrieval/ppr.py`, `core/retrieval/extractor.py`, `core/retrieval/pipeline.py`, `core/pulse/memory.py`, `core/pulse/context.py`, `core/pulse/entity_resolver.py`, `core/pulse/graph.py`, `core/lib/graph_rules.py`, `core/context/` (registry), `core/skills/brain_synth_v2.py`, `core/pulse/patterns.py` |
| **Presentation** | `core/pulse/briefing.py`, `core/pulse/decision_pulse.py`, `core/pulse/sentinel.py`, `core/pulse/models.py`, `core/pulse/pipeline.py`, `core/pulse/run_logger.py`, `scripts/run_health.py` |
| **Infrastructure** | `core/services/db.py`, `core/services/google_service.py`, `core/services/push_notification.py`, `api/index.py`, `.github/workflows/` |
|  |  |

> **Note:** The webhook pipeline flow and task lifecycle state machine are documented in separate files: [docs/webhook-pipeline.md](webhook-pipeline.md) and [docs/task-lifecycle.md](task-lifecycle.md).
