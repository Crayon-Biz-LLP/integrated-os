# 14. The Pulse Engine — Prompt Architecture & Context Assembly

> **⚠️ LEGACY WARNING**: This file describes the **current** prompt architecture. The original "5 parallel AI agents + ToolRegistry multi-turn agent loop" architecture was replaced in Part 58 with a single-LLM-call pipeline. For the historical architecture, see `58-final-architecture-overhaul.md`.

## Overview

The Pulse Engine now uses a **single LLM call** with rich pre-assembled context. Instead of running 5 parallel agents that each return text blocks for injection into a 250-line prompt, the system collects all data upfront into a `BriefingContext` dataclass and assembles a structured prompt from typed fields.

## Prompt Files

All prompts are centralized in `core/prompts/`:

| File | Purpose |
|------|---------|
| `voice.py` | Rhodey's character bible — defines tone, vocabulary, personality |
| `briefing.py` | Daily briefing prompt assembly (calendar, tasks, context) |
| `query.py` | User query/interrogation prompt (with CONTEXT_SECTION_RULES) |
| `classify.py` | Intent classification prompt |
| `guards.py` | Shared guardrail injection for all prompts |
| `workflow.py` | Enrichment queue and batch workflow prompts |
| `planner.py` | Action Planner prompt for structured operations |
| `ingest.py` | Channel-specific ingestion prompts (email, WhatsApp ingest) |

## Context Assembly (Not "5 Parallel AI Agents")

The old architecture ran 5 specialized AI agents in parallel that each returned text blocks for prompt injection. The current architecture replaces these with **deterministic data collection** via the BriefingContext dataclass:

| Old Agent | Current Replacement |
|-----------|-------------------|
| **Dependency Agent** (`check_task_dependencies`) | Replaced by `dependency_context` built from `graph_edges` DEPENDS_ON query — deterministic SQL, no LLM call |
| **Social Graph Optimizer** (`analyze_communication_patterns`) | Replaced by `social_graph_context` from graph node stats — deterministic |
| **Temporal Pattern Detector** ("On this day" memories) | Still exists as `hindsight_empty` / `hindsight_context` — but data-driven, not an LLM agent |
| **Serendipity Engine** (`find_serendipity_paths` RPC) | Still exists as recursive CTE — pure SQL, ~50ms |
| **Graph Centrality** (`get_most_connected_nodes` RPC) | Still exists as RPC — pure SQL |
| **Adaptive Briefing Learner** | Removed — the pattern learner (`planner_critic.py`) handles meta-learning at the system level |

## The Briefing Prompt Structure

The `build_briefing_prompt()` function in `core/prompts/briefing.py` assembles the prompt from these sections:

### 1. System Persona (Mode-Specific)

```python
system_persona = PERSONA_MORNING  # or AFTERNOON, CLOSING_LOOP, WEEKEND_REST
```

Each mode has a distinct tone:
- **MORNING**: Delta-focused — "what happened overnight, what's the one pivot"
- **AFTERNOON**: Velocity-focused — "what's moving or stalled"
- **CLOSING LOOP**: Hand-off focused — "one sentence on the last closed loop"
- **WEEKEND REST**: Light — "keep it about home and church"

### 2. Mode Overrides

- **URGENT mode**: Only Work and Done sections. Home, Church, Ideas hidden.
- **NIGHT mode**: Schedule, Done, Home, Church, Work (top 2-3), Ideas.

### 3. Data Sections

Calendar events, active tasks (with urgency sorting), overdue tasks, serendipity context, hindsight memories, people context, practices/rhythms, resources (only on weekdays), and strategic season context.

### 4. Voice Injection

The `voice.py` prompt is injected as the closing instruction — it defines Rhodey's tone:
- Direct, human, no CEO-speak
- First sentence answers the question directly
- Emoji for task bullets only, not for narrative
- No PART 1 / PART 2 labels in user queries
- Varied natural confirmations instead of receipts ("Got it → Task logged")

## User Query Prompt

For user questions routed to `interrogate_brain()`, the prompt in `core/prompts/query.py` uses `CONTEXT_SECTION_RULES` to prevent the LLM from mislabeling sections:

```
CONTEXT_SECTION_RULES:
- **ACTIVE TASKS**: Current pending tasks — Danny's live to-do list.
- **RECENTLY COMPLETED TASKS**: Closed/completed. Historical — do NOT list as current.
- **RELEVANT MEMORIES / HINDSIGHT MEMORIES**: Historical records. Context only.
- **TACTICAL MAP / SERENDIPITY**: Graph-derived connections. Background context.
- **ALL OTHER SECTIONS**: Supporting context only.

Only what's under ACTIVE TASKS represents current workload.
```

## Tone & Voice Evolution

The prompt architecture has been through several refinements:

| Phase | Approach | Status |
|-------|----------|--------|
| Pre-Part 57 | 250-line monolithic prompt with 30+ hard constraints | ❌ Replaced |
| Part 58 (Refactor) | Structured prompt with `BriefingContext`, single LLM call | ✅ Current |
| Part 61 (Voice) | Separated `voice.py` character bible, removed PART 1/PART 2 labels, varied confirmations | ✅ Current |

## Key Guardrails

- **URL quarantine**: Text containing URLs is saved as resource only, never memory or graph entity
- **Data fidelity**: The LLM is STRICTLY FORBIDDEN from listing tasks not in the SYSTEM_TASKS list
- **Section boundaries**: Historical sections (completed tasks, memories) are clearly annotated to prevent LLM confusion
- **No hallucination**: Fact-only verification in sentinel prompts
