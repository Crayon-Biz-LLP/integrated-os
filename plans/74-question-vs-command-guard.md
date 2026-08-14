# 74 — Question-vs-Command Guard: "Is X sorted?" must not close a task

**Status:** Spec only — not implemented (2026-08-14)
**Canonical vision:** `product-summary/00-vision-and-mindset.md`
**Architecture home:** `product-summary/99-architecture-reference.md` (Layer 3 Intelligence — Action Planner/Executor)
**Related:** `plans/73`, `db/100` (per-item undo, Layer 1+2 — implemented)

---

## 1. Decision record

On Aug 14 a week-old Teams message — *"Is thsi sorted?"* — was approved (accidentally, from the
app). The approval ran the full Action Planner pipeline and the plan contained a single
`close_task` on the "Work on the pending Ashraya accounting issues" task, which it closed without
any confirmation. The ack that followed ("Done — Confirm if issue is sorted is off your plate")
was additionally mislabeled (title came from the LLM's `human_label`, not the task's real title).

**Root cause:** a *question* was interpreted as a *completion command*. The executor executes
`close_task` as a first-class action with no distinction between:

- a user explicitly saying *"mark the Ashraya task done"* (a command), and
- an approved item whose body is a question/status check (*"is it sorted?"*, *"done?"*, *"by
  tomorrow?"*) where the LLM *infers* "this means close the task".

The pipeline already has confirmation machinery for closures
(`ask_task_update_confirmation`, used for `reschedule`/`modify_recurring`) — `close_task` from an
ambiguous message bypasses it. The per-item undo (Layer 1+2, `db/100` + `/api/decisions/undo`)
makes this *recoverable*; this plan makes it *not happen silently in the first place*.

**Decisions:**

1. **Classification gate:** when an approval's plan contains `close_task`
   (`cancel_recurring`/`suppress_instance` too), the executor's confirmation path must engage for
   *ambiguous* sources — specifically when the source text is a question/status check rather than
   an explicit completion statement.
2. **Deterministic first pass, LLM second:** the cheapest reliable signal is the message's
   **intent/classification** already computed at ingest (question vs completion vs task). A
   `query_info`-style question must not silently produce `close_task`. Gate on a deterministic
   "is this a question?" check first; only fall back to an LLM judgment when ambiguous.
3. **Confirmation, not a wall:** when the gate fires, the pipeline does NOT fail-closed and does
   NOT execute — it routes to the existing `ask_task_update_confirmation`-style ask ("Did you mean
   to close *Work on the pending Ashraya accounting issues*?") and parks the item. This is the same
   pattern `reschedule` uses, so it is in-architecture, not new ad-hoc code.
4. **Ack-title honesty (bundled):** `close_task` acks must render the **actual task title**
   (`tasks.title`), not the LLM's `human_label`. The phantom-task confusion came from ack text that
   named a different title than the task that was closed.
5. **Explicit-command bypass:** when the user's message is an explicit completion statement
   ("mark X done", "I've finished X"), the gate does not fire — commands stay one-tap.

---

## 2. Key verified facts this spec rests on

- `_process_channel_pending_decision` (`core/webhook/utils.py`) and the email path
  (`core/webhook/email.py`) run `plan_actions` → `execute_planned_actions` with `intent="TASK"`
  — no completion/question classification is consulted before execution.
- `execute_planned_actions` executes `close_task` directly
  (`core/actions/executor.py` ~line 689); `compensate_action` (same file) is the reversal path the
  undo endpoint reuses — meaning the gate is a *pre*-execution guard, not a post-hoc fix.
- The executor already tracks `has_closures` and already has `ask_task_update_confirmation`-style
  confirmation machinery for other operations (`models.py` `NeedsClarification`,
  `core/lib/clarification_state.py` session machinery).
- The classifier at ingest already produces an intent; question-pattern messages historically
  classify as `COMPLETION`/`TASK` ambiguously — this is the gap the gate closes.
- `decisions.metadata.actions` (`db/100`) records every committed action, so a gated-and-confirmed
  close remains undoable.

---

## 3. Design

### 3.1 Deterministic question gate (first pass)

In `execute_planned_actions`, before executing a plan that contains `close_task`
(`suppress_instance`/`cancel_recurring`), run a cheap deterministic check on the original `text`:

- trailing `?`, question lead-ins ("is", "are", "did", "have", "can", "when", "who", "what",
  "why", "how", "done?"/"sorted?"/"ready?"), or a classifier intent of `QUESTION`.
- If the text is question-shaped **and** the plan's only actions are closures → route to
  confirmation (3.3). If the text is question-shaped but the plan also creates/notes something,
  keep those, and gate only the closures.

**Why deterministic first:** the LLM already failed us once by reading a question as a command;
asking the LLM "is this a question?" reintroduces the same flake. A small pattern set is
testable and predictable. Ambiguous text (no pattern match) may use an LLM judgment call as a
second pass, but the plan's primary path is deterministic.

### 3.2 Classification intent hook

`plan_actions` receives `intent` from callers. Introduce a distinct intent for
question/status-check messages at ingest (e.g. reuse `COMPLETION` for *explicit* completions and
add a `QUESTION`/`STATUS_CHECK` for questions) and have the gate key off it — the classifier
already exists and learns from observations (`emit_observation`), so this closes the learning
loop too: when a gated close gets confirmed, the classifier gets the correction signal.

### 3.3 Confirmation routing (in-architecture)

Gate → `ask_task_update_confirmation`-style ask (existing machinery) naming the **actual task
title**; the user's Yes/No resolves via the existing confirmation-session flow. A "No" leaves the
task untouched and the message is logged as rejected/noted (Guards 1/3 already handle the
fallback-note path — zero data loss).

### 3.4 Ack-title honesty

`close_task`'s rendered ack must source the title from the task row, not
`action.human_label`. (See `core/lib/rhodey_voice.py` `ACK_INTENTS`/`render_acks` — the ack
renderer keys off intent + title; feed it the task's real title.)

---

## 4. Explicitly out of scope (recorded, not forgotten)

- Reversing *sent* messages (Telegram can't unsend — a correction ack on undo is the honest
  bound; `decisions/undo` already reports `actions_not_reversed`).
- Gating `create_task`/`create_event` (creating something from an approved item is the intended
  approve behavior — only *destructive* closures get the gate).
- Changing the app's approve/reject UI (Layer 4 affordance — separate concern).

---

## 5. Verification plan (when implemented)

- Unit: deterministic question gate (pattern table incl. "Is thsi sorted?", "done?", "by
  tomorrow?", explicit "mark X done" bypass); gate does not fire on non-closure plans.
- Unit: confirmation-session round-trip (gate → ask → Yes closes / No leaves pending).
- Unit: ack-title test — close a task, assert ack contains `tasks.title`, not `human_label`.
- Integration: TestClient on `/api/teams-action` with a question-shaped body → no close, ask
  recorded; explicit completion body → closes, no ask.
- Gate: ruff, full unit suite, `flutter analyze`, reference sweep.
