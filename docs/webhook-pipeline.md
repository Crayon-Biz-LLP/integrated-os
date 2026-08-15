# Rhodey OS — Webhook Message Pipeline

```mermaid
%%{init: {'theme': 'neutral', 'themeVariables': { 'primaryColor': '#1a1a2e', 'primaryTextColor': '#e0e0e0', 'primaryBorderColor': '#4a4a6a', 'lineColor': '#6b6b8a', 'secondaryColor': '#16213e', 'tertiaryColor': '#0f3460' }}}%%

flowchart LR
    subgraph Ingest["Ingestion"]
        W[Webhook handler.py] --> D[Dedup update_id/dedup_key]
        D --> U[URL Quarantine url_filter.py]
        U --> C[Classify Gemini Flash Lite]
        C --> WO[Workflow Overlap topic guard]
    end

    subgraph Plan["Action Planning"]
        WO --> PA[plan_actions candidate assembly]
        PA --> VO[validate_operation LLM validation]
        VO --> EA[execute_planned_actions]
    end

    subgraph Exec["Execution"]
        EA --> EL[Entity Linker resolve BEFORE create]
        EL --> DW[DB Write create_task_direct]
        DW --> SM[State Machine guard_require_valid_transition]
        DW -.-> GS[Google Sync Calendar + Tasks]
        EA --> R[Send Response send_telegram]
    end

    subgraph Async["Async Cold-Start-Safe Queue"]
        R --> EQ[Enrichment Queue pending_enrichment_jobs]
        EQ --> GE[Graph Edges pending_graph_edges HITL]
        EQ -.-> IM[Index Memory retrieval index queue]
    end

    subgraph Error["Error Handling"]
        DW -.-> CP[compensate_action rollback]
        CP --> DLQ[Dead Letter Queue 3 retries]
        EA -.-> DLQ
    end

    Ingest --> Plan
    Plan --> Exec
    Exec --> Async
    Exec -.-> Error
```

## Key Properties

| Property | Detail |
|---|---|
| **Single routing path** | All paths route through `plan_actions() → execute_planned_actions()` |
| **No legacy pipeline** | `process_single_dump` — fully removed |
| **Queue-based enrichment** | Survives serverless cold starts (not fire-and-forget) |
| **HITL for graph edges** | All edges go through `pending_graph_edges` approval table |
| **State machine guards** | On ALL status transitions |
| **DLQ with escalation** | 3 retries before escalation |
| **Tenant-scoped auth** | Incoming chat must match the bound tenant (`users.telegram_chat_id`); test harness uses `TEST_CHAT_IDS` allow-list, default-off |
| **Direction-aware** | Own-sends (email/Teams/Outlook) filtered via `direction` — never surface as inbound |
| **Snooze-aware** | Items can be snoozed (`snooze_count/snoozed_until/snooze_feedback`) and escalate via `awaiting_reply` |
| **Decisions ledger** | Every approve/reject/snooze/undo persists to `decisions` with learn features |

## File Map

| Stage | Key Files |
|---|---|
| Ingestion | `core/webhook/handler.py`, `core/webhook/dispatch.py`, `core/webhook/classify.py`, `core/prompts/classify.py`, `core/lib/url_filter.py` |
| Action Planning | `core/actions/planner.py`, `core/actions/models.py` |
| Execution | `core/actions/executor.py`, `core/pulse/tools.py`, `core/lib/entity_linker.py`, `core/lib/state_machines.py` |
| Async | `core/lib/enrichment_queue.py`, `core/pulse/graph.py`, `core/retrieval/pipeline.py` |
| Error Handling | `core/skills/dlq_consumer.py`, `core/actions/executor.py` |
