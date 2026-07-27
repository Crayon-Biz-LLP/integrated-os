# Recent Bug Fixes & Enhancements (Jul 27, 2026)

## 1. TASK Interception Reverted

**Problem:** Clear TASK commands ("Remind me to purchase the Ashraya domain") at `confidence=1.0` were unnecessarily asking "📋 I found these items... Want me to handle them?" because `executor.py` intercepted both NOTE AND TASK intents.

**Fix:** Changed `if intent in ("NOTE", "TASK"):` to `if intent == "NOTE":` in `execute_planned_actions()`. TASK intents now execute immediately. NOTE intents still intercept extracted tasks for user confirmation.

**File:** `core/actions/executor.py`

**Commits:** `2baf97c`

---

## 2. 5 Pipeline Bug Fixes

### Bug 1: Missing Date / Dropped Calendar Event
**Root Cause:** Workflow execution loop explicitly dropped the LLM-parsed `reminder_at` when executing `task_imperative` signals. The date was extracted but never passed to `create_task_direct()`.
**Fix:** Added `reminder_at=reminder_at` keyword argument to `create_task_direct()` call.
**File:** `core/webhook/workflows.py`

### Bug 2: Dropped Google Task ID
**Root Cause:** During Action Planner rewrite (Jul 15), the `sync_to_google()` return value was not persisted to the `tasks` table. Downstream completion sync couldn't find the Google Task.
**Fix:** Capture return value of `sync_to_google()` and execute Supabase `update()` to persist `google_task_id`.
**File:** `core/pulse/tools.py`

### Bug 3-5: Missing `await` on `extract_and_link_entities()`
**Root Cause:** Three call sites called `async def extract_and_link_entities()` without `await` — the coroutine was created but silently garbage collected. Entity extraction for WhatsApp FYI notes and unified ingest paths never ran.
**Fix:** Added `await` to:
- `core/skills/whatsapp_ingest.py:187` — WhatsApp FYI notes
- `core/lib/ingest.py:144` — NOTE ingest branch
- `core/lib/ingest.py:208` — FYI/actionable ingest branch

---

## 3. "Remind me" Pre-Filter Added

**What:** A deterministic regex pre-filter in `classify.py` detects "remind me to/that/about" patterns and returns `intent=TASK, confidence=1.0` without any LLM call.

**Why:** Prevents COMPLETION misclassification (the word "purchase" could trigger the completion keyword matcher) and saves an LLM call.

**File:** `core/webhook/classify.py`

---

## 4. Deferred: `handle_confident_note` session_id/active_anchor gap

**Issue:** Notes created via `/note` shortcut don't have `thread_id`/`thread_entity_name` stored in memory metadata.

**Fix identified:** Add `session_id=session_id, active_anchor=active_anchor` to `create_note_direct()` call at `handler.py:77`.

**Status:** Deferred to future session.
