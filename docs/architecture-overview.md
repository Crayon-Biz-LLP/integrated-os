# Rhodey OS — 6-Layer Architecture

```mermaid
%%{init: {'theme': 'neutral', 'themeVariables': { 'primaryColor': '#1a1a2e', 'primaryTextColor': '#e0e0e0', 'primaryBorderColor': '#4a4a6a', 'lineColor': '#6b6b8a', 'secondaryColor': '#16213e', 'tertiaryColor': '#0f3460' }}}%%

flowchart TB
    subgraph L1["Layer 1 — Ingestion"]
        I1[Telegram / WhatsApp / Email / Teams / Calls]
        I2[classify.py - Gemini Flash Lite]
        I3[url_filter.py - URL Quarantine]
        I4[ingest() - Unified Contract]
        I5[Dedup - update_id / dedup_key / DB]
        I1 --> I2 --> I3 --> I4 --> I5
    end

    subgraph L2["Layer 2 — Processing"]
        P1[plan_actions() - Action Planner]
        P2[execute_planned() - Executor]
        P3[Entity Linker - resolve BEFORE create]
        P4[Enrichment Queue - pending_enrichment_jobs]
        P5[DLQ Consumer - 3-retry dead letter]
        P6[State Guards - 16 state machines]
        P1 --> P2
        P2 --> P3
        P2 -.-> P4
        P2 -.-> P5
        P2 --> P6
    end

    subgraph L3["Layer 3 — Intelligence"]
        S1[Associative Retrieval - 7-signal + PPR]
        S2[Knowledge Graph - 5 types / 16 edges / HITL]
        S3[Context Registry - 6 strategies + gates]
        S4[Brain Synthesis - Org-level canonical pages]
        S5[Pattern Detection - Memory clustering]
        S1 --> S2
        S1 -.-> S3
        S2 -.-> S4
        S3 --> S4
    end

    subgraph L4["Layer 4 — Presentation"]
        R1[Pulse Engine - Single LLM call]
        R2[Decision Pulse - AI-free approvals]
        R3[Sentinel - Meeting alarms + piggybacks]
        R4[Health Monitor - run_full_health_check()]
    end

    subgraph L5["Layer 5 — Persistence"]
        D1[State Machines - 16 tables / valid transitions]
        D2[DB-Backed State - survives cold restarts]
        D3[Temporal Lineage - DB trigger versioning]
        D4[pending_nodes + merge_proposals]
    end

    subgraph L6["Layer 6 — Integration"]
        X1[Google Calendar - 404 auto-heal sync]
        X2[Google Tasks - Status propagation]
        X3[Telegram Bot - FCM push notifications]
        X4[cron-job.org - Sentinel / DPulse / Roundup]
        X5[GitHub Actions - Pulse / Backfill / Health]
    end

    %% Cross-layer connections - data flows top-to-bottom
    L1 -->|classified intent| L2
    L2 -->|memory index| L3
    L3 -->|strategic context| L4
    L2 -.->|persist to DB| L5
    L4 -->|dispatch| L6
    L2 -.->|sync external| L6
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

## File Map

| Layer | Key Files |
|---|---|
| Ingestion | `handler.py`, `dispatch.py`, `classify.py`, `url_filter.py`, `ingest.py`, `prompts/classify.py` |
| Processing | `planner.py`, `executor.py`, `models.py`, `tools.py`, `entity_linker.py`, `enrichment_queue.py`, `dlq_consumer.py` |
| Intelligence | `search.py`, `ranking.py`, `ppr.py`, `extractor.py`, `pipeline.py`, `memory.py`, `context.py`, `entity_resolver.py`, `brain_synth_v2.py`, `patterns.py` |
| Presentation | `briefing.py`, `decision_pulse.py`, `sentinel.py`, `models.py`, `pipeline.py`, `api/briefing.py` |
| Persistence | `state_machines.py`, `db.py`, `graph_rules.py`, `db/*.sql` |
| Integration | `google_service.py`, `telegram.py`, `push_notification.py`, `.github/workflows/*` |
