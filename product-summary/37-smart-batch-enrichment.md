# Smart Batch Enrichment

## Problem
The post-capture enrichment pipeline could only promote ONE actionable signal per message. For messages with multiple signals (e.g., "Send proposal by Monday AND meeting at 11am"), only the highest-priority signal was acted on. The rest were stored in memory metadata but never surfaced to the user.

## Solution
Replaced single-signal promotion with **smart batch collection** — enrichment now collects ALL `calendar_event`/`deadline`/`task_imperative` signals and gives the user one message listing every item. The LLM then parses per-signal decisions (confirm/decline/skip) from a single reply.

## Architecture

### Signal Collection (`core/webhook/dispatch.py:_run_post_capture_enrichment`)
- Iterates ALL signals (no `break` after first match)
- Collects calendar_event, deadline, task_imperative into `actionable` list
- For calendar_event/deadline, populates `reminder_at` from LLM output (ISO 8601) or falls back to `_resolve_calendar_datetime()`
- Creates ONE `batch` workflow with `{"signals": [...]}` payload
- Followup message lists every item with index number

### Decision Parsing (`core/prompts/workflow.py:build_workflow_resume_prompt`)
- Batch mode: lists signals by index, asks per-signal confirm/decline/skip
- LLM instructed for partial approval ("yes for meeting, no for deadline") and catch-all ("yes" = confirm all)
- Returns `{"decisions": [{"index": 0, "decision": "confirm"}, ...]}`
- Backward compat: single-signal workflows use original prompt

### Execution (`core/webhook/workflows.py:check_and_resume_workflow`)
- Deterministic fast path: "yes"/"sure" = confirm all; "no"/"cancel" = decline all
- LLM slow path: parse per-signal decisions
- Per-signal execution: iterates confirmed indices, creates one task per signal
- `process_single_dump()` handles task creation + internal `accumulate_action()` — no duplicate accumulation
- Title fallback chain: `task_title → proposed_title → title → "New Task"`

## Signal Types

| Type | Description | Action |
|------|-------------|--------|
| `calendar_event` | Scheduled meeting/call at specific time | Task with `reminder_at` ISO datetime |
| `deadline` | Hard deadline for deliverable | Task with `reminder_at` |
| `task_imperative` | Explicit directive ("I need to...") | Task without reminder |
| `person_intro` | New person with org/role | Informational (stored in metadata) |
| `financial` | Quote, budget, cost | Informational |
| `dependency` | Multi-step planning | Informational |

## Enrichment Prompt (`core/prompts/workflow.py`)
- `Current time: {IST datetime}` — enables LLM to resolve "Monday" → `2026-07-13`
- `calendar_event` signal type with `reminder_at` ISO 8601 field
- `duration_minutes` for events

## Key Files
- `core/prompts/workflow.py` — Batch + single-signal prompts with per-signal decisions
- `core/webhook/dispatch.py` — Multi-signal collection, batch workflow creation
- `core/webhook/workflows.py` — Batch handler with deterministic/LLM per-signal execution
