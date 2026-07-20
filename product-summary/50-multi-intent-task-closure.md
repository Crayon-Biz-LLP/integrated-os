> **⚠️ LEGACY WARNING**: This file references architecture from an earlier phase. Some modules mentioned (e.g., handle_confident_task, process_single_dump, quick_process, janitor) have been replaced or removed in Parts 57-61. The core concept remains valid — see 58-final-architecture-overhaul.md for current architecture.
# Multi-Intent Messages & Task Closure Pipeline

## Problem

Rhodey's Telegram webhook had a **single-intent bottleneck**: `classify.py` returns one intent → `route_by_intent` is a 1:1 switchboard. Messages with mixed actions (e.g., "Not needed, but close the Amita tasks") got only the dominant intent processed. The workflow handler consumed compound reply messages without passing ancillary text through for classification.

Reported issue: "Not needed.. You can just close the open tasks related to Amita and FC Madras." → Rhodey replied "Cancelled." and dropped the task-closure intent entirely.

## Root Cause

Two independent gaps:

1. **Gap A — Workflow handler ate compound reply messages** (`core/webhook/workflows.py`): `check_and_resume_workflow` returned a boolean — if True, handler skipped classify entirely. Compound messages like "not needed but also close X" had their task-closure intent lost.

2. **Gap B — Enrichment pipeline blind to task_closure signals** (`core/prompts/workflow.py` enrichment prompt): The enrichment prompt only collected `calendar_event`, `deadline`, and `task_imperative` signals. `task_closure` was not a recognized signal type.

3. **Gap C — Classify prompt had no multi-intent support** (`core/prompts/classify.py`): Messages with multiple intents (decline + close tasks) got a single intent. No `secondary_actions` concept existed.

4. **Gap D — Bulk close syntax** ("close the open tasks") needed `_process_task_closure` to resolve which tasks to close by fuzzy matching entity references.

## Solution

Extended the **existing Smart Batch Enrichment** pipeline (from Part 21) rather than switching to the heavy ToolRegistry agent loop. Two-pronged fix:

### A — Workflow handler returns ancillary text

Changed `check_and_resume_workflow` return from `bool` to `Tuple[bool, Optional[str]]`. If the batch confirm/decline consumed the workflow intent but ancillary text remains (e.g., "Cancelled. But also close the Amita tasks."), the handler falls through to normal classify/dispatch with the extracted text.

### B — task_closure signal in enrichment prompt

Added `task_closure` to the enrichment prompt's signal types with `target_task_description` field. The `_run_post_capture_enrichment` collector now collects task_closure signals alongside existing signals.

### C — Multi-intent via secondary_actions (classify prompt)

Added two rules to `classify.py`:
- **TASK MANAGEMENT DIRECTIVES**: Imperative close/cancel language ("close the tasks", "cancel that", "mark done") classified as COMPLETION
- **SECONDARY ACTIONS**: When message has multiple intents, classifier populates `secondary_actions` array in JSON with intent + reasoning for each. `route_by_intent` processes these after the primary handler with a 0.5 confidence threshold.

### D — _process_task_closure helper

Shared helper in `dispatch.py` that fuzzy-matches entity names against open task titles. Used by both the batch enrichment executor (Gap B path) and `route_by_intent` secondary_actions (Gap C path).

## Files Changed

- `core/prompts/classify.py` — TASK MANAGEMENT DIRECTIVES rule, SECONDARY ACTIONS rule, `secondary_actions` JSON schema field
- `core/prompts/workflow.py` — `task_closure` signal type + `target_task_description` in enrichment prompt; `has_other_content` in batch resume prompt
- `core/webhook/dispatch.py` — `_process_task_closure` helper; `route_by_intent` secondary_actions processing; enrichment collector for task_closure
- `core/webhook/workflows.py` — `check_and_resume_workflow` returns `Tuple[bool, Optional[str]]`; task_closure execution in batch confirm loop
- `core/webhook/handler.py` — tuple handling from workflow; ancillary text re-route to classify

## Edge Cases Handled

- **QUERY + action** ("Who is Amita and close her tasks"): QUERY handled as primary, COMPLETION as secondary_actions
- **Batch enrichment with closure**: Multiple enrichment signals + task_closure in one message
- **No open tasks match**: Graceful skip, no crash
- **Multiple entities** ("close Amita and FC Madras tasks"): Both entity sets resolved independently

## Key Decisions

- **Extended enrichment pipeline, not the Pulse agent loop**: The ToolRegistry agent loop is heavy (multiple LLM calls). Enrichment pipeline already had the batch approval UI — extending it was the smallest change.
- **secondary_actions in classify, not a new intent type**: Adding a new COMPOUND intent would require a new routing path. A secondary_actions array on existing intents is simpler and backward-compatible.
- **Fuzzy match over exact task ID**: Users say "tasks related to Amita" not "task IDs 1736, 1737". The helper matches entity names against task titles via substring/ILIKE.
