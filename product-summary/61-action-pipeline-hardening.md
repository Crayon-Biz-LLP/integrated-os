# 61 - Action Pipeline Hardening (Fail-Closed Contracts)

> **Status:** Plan approved — **all five phases implemented + learning-loop
> writeback** (S19b negative UAT added; M9.4 planner gate re-regenerated green;
> briefing golden drift is pre-existing). Full suite: 569 passed, 120 skipped
> (live-DB), 2 pre-existing `test_tenant_scope` failures (verified via stash).
> **Scope:** `core/actions/*`, `core/webhook/dispatch.py`, `core/prompts/planner.py`,
> `core/lib/time_utils.py`, `core/webhook/workflows.py`, `core/llm/*`.
> **Canonical architecture home:** Layer 2 (Processing), Layer 5 (Persistence),
> Layer 6 (Integration) per `99-architecture-reference.md`.

## Motivating Incident (Aug 12, 2026)

User: *"Defer the Ashraya domain purchase by 7 days"* → Task 2466.

1. Classifier and routing correctly matched the message to Task 2466 and chose
   `reschedule` — the right operation.
2. The planner LLM, asked to compute an absolute timestamp for "by 7 days,"
   failed to do the calendar math and emitted `reschedule` **without**
   `new_reminder_at`. The prompt's time rules cover `"today 3pm"`, `"tomorrow"`,
   `"next Friday 2pm"` — not relative deltas — and its last rule is
   *"If no time is given, return null for reminder_at. Do not invent a time."*
3. The executor's reschedule handler hit the branch at `executor.py:616`:
   `# No new time provided — just acknowledge` → `closed_ids.append(...)`.
4. The user received **"✅ Closed: …"** — a success ack with zero database
   writes. `reminder_at` stayed on its old date.

Two root flaws, both structural:

- **A. The LLM is asked to do arithmetic it's bad at** (calendar math), while
  the system already owns a deterministic date resolver (`resolve_relative_dates`,
  `core/lib/time_utils.py`) it only uses for memory context — not the planner.
- **B. The executor is fail-open by design**: a malformed action is *acknowledged*
  rather than rejected or questioned. This is the vision doc's **"lie button"**
  anti-pattern at message level — a promise with no write behind it.

The fixes are not patches for this one message. They establish invariants that
make the failure class impossible.

## The Invariants

| # | Invariant | Failure class killed |
|---|---|---|
| 1 | **No success without a write.** Every ack derives from a confirmed DB write; a plan that can't be executed is reported or clarified, never acknowledged. | The silent-ack "lie" (executor.py:616) |
| 2 | **The LLM never does time arithmetic.** It extracts structured deltas or absolute dates; code computes timestamps in the tenant timezone. | Wrong/missing dates from "defer by 7 days" class |
| 3 | **Actions are typed and validated at the plan boundary.** Per-op schemas with required fields; a malformed plan never reaches the executor. | Loose `params: dict` reaching the DB |
| 4 | **The executor applies deltas only.** Merge with the existing row; a missing field preserves state. | `{"recurrence": None}` overwrites (modify_recurring) |
| 5 | **Clarifications are stateful and resume.** Pending action + original text persist (Layer 5 `conversation_workflows`); the reply resumes it; the exchange feeds the learning loop. | Dead-end questions; "asked but reset" |
| 6 | **Shape enforced at the LLM boundary.** Schema threaded through Gemini `responseSchema` + OpenRouter `json_schema`. | Half-formed JSON from the LLM |

## Layer Mapping (why this is a hardening, not a divergence)

All phases sit in the existing 6-layer architecture and exercise only
mechanisms it already documents:

| Phase | Layer(s) | Files (already mapped in 99-architecture-reference.md) |
|---|---|---|
| 1 Typed contracts | L2 Processing | `core/actions/models.py`, `planner.py`, `executor.py` |
| 2 Deterministic time | L2 Processing + shared lib | `planner.py`, `prompts/planner.py`, `core/lib/time_utils.py` (already used at L3) |
| 3 PATCH semantics | L2 Processing | `executor.py` |
| 4 Stateful clarification | L2 Processing + L5 Persistence (+ L3 learning) | `core/webhook/workflows.py` (L2), `conversation_workflows` table (L5), learning tables (L3) |
| 5 Provider shape | L6 Integration | `core/llm/providers.py`, `fallback.py` |

The architecture doc already describes the Action Model as **"Typed action
execution with validation"** — Phase 1 makes that claim true. Per
`58-final-architecture-overhaul.md`, the pre-refactor `engine.py` **already used
5 Pydantic models**; the dataclass `Action` regression is what we're restoring.

## Phases

### Phase 1 — Typed contracts (the keystone) — *in progress*

- Convert `Action` to a Pydantic base model (`extra="allow"`) with 12 per-op
  subclasses discriminated by `operation` (`PlannedAction` union). Legacy
  construction (`Action(..., params={...})`) keeps working via a before-validator
  that unpacks `params` into fields and a `params` computed property that remains
  the executor's read channel (incl. `_created_*` rollback bookkeeping).
- Required-field semantics per op:
  - `reschedule`: `new_reminder_at` **required** (datetime, parse-validated).
  - `modify_recurring`: **at least one** of `new_rrule` / `new_reminder_at`.
  - `update_metadata`: **at least one** of `new_priority` / `new_deadline`.
  - `create_task`/`create_note`/`create_event` title/content stay Optional at the
    model level so the executor's `human_label`/`text` fallbacks are preserved;
    the existing executor `validate_operation` catches the truly-empty case.
  - Target-ID ops rely on the existing executor `validate_operation` (task
    existence is a runtime concern, not a schema one).
- `plan_actions()` constructs typed models from LLM JSON via the union
  `TypeAdapter`; a `ValidationError` raises `NeedsClarification` (defined in
  `models.py`) carrying operation / missing fields / original text.
- `NeedsClarification` caught in `core/webhook/dispatch.py` (both `plan_actions`
  call sites) → routed to `handle_clarification` (stateless until Phase 4).
  Email/approval paths already wrap `plan_actions` in try/except → surface as
  processing failure (fail-closed).
- `validate_operation` extended with per-op required-param checks **before** the
  DB existence check (so they're unit-testable without a DB), plus a
  `format_rfc3339` parseability check (a present-but-unparseable date currently
  writes `reminder_at: None`).
- The silent-ack branch (executor.py:616) becomes a **failed action** — the
  partial-failure Telegram path, never `closed_ids`.
- Tests: `tests/unit/test_action_models.py` (12 DB-free tests — required-field
  matrix, union discrimination, params compat incl. `_created_*` bookkeeping,
  `action_param_error`, NeedsClarification payload), and UAT scenario **S19b**
  (reschedule without time must leave `reminder_at` untouched).

### Phase 2 — Deterministic time — *implemented*

- **`TimeDelta` schema** (`{amount > 0, unit: days|weeks|hours, direction: later|earlier}`)
  on `reschedule` / `modify_recurring`; the model validator calls
  `resolve_time_delta()` (new in `core/lib/time_utils.py`) to compute the
  absolute timestamp in the tenant zone — the LLM extracts the delta, the code
  does the arithmetic. An explicit `new_reminder_at` always wins over a delta.
- **`resolve_relative_dates` extended** with `"in/by N days/weeks"`, `"next week"`,
  `"in a week"`, `"a week from now"` (the Aug 12 phrasing "by 7 days" now
  resolves deterministically).
- **Prompt**: `build_planner_prompt` gained a `resolved_dates` param rendering a
  `RESOLVED_RELATIVE_DATES` section ("- None detected." when empty) plus
  `time_delta` instructions for `reschedule`/`modify_recurring`. The raw `text`
  is never rewritten (notes/fallback content + lexical pre-filter depend on it).
- `tests/golden/planner_tenant1.txt` regenerated from the M9.4 fixture — gate
  green; `classify_tenant1.txt` untouched.
- **Deterministic backstop (found by live verification):** live smoke testing
  caught the real LLM emitting `reschedule` with `params: {}` (no time) even
  with the resolved date in the prompt — the flake the typed contract then
  correctly rejected. To make the common case structural rather than
  caught-after-the-fact: `extract_time_delta(text)` (new in time_utils.py —
  `"by/in N days|weeks|hours"`, `"N days from now"`, `"push it back a week"`,
  `"two more weeks"`, `"move it up 2 days"`) and
  `inject_deterministic_delta(action, text)` (models.py) run in `plan_actions`
  BEFORE typed validation: a time-bearing op (`reschedule`/`modify_recurring`)
  with no time gets the delta injected from the raw text. The LLM is a
  suggestion layer for time; the code is the authority (invariant #2). No
  computable delta → action unchanged → fail-closed ask still applies.
- Tests: `tests/unit/test_time_utils.py` (delta math + phrase resolution +
  `extract_time_delta`), `time_delta` + `inject_deterministic_delta` cases in
  `tests/unit/test_action_models.py`.

### Phase 3 — PATCH semantics — *implemented*

- **`modify_recurring_updates()`** (executor): builds the tasks-table patch from
  present deltas only — a time-only change no longer writes
  `{"recurrence": None}` (the data-loss class). Calendar sync passes the
  **effective** rrule (`new_rrule or existing`) so a time-only change preserves
  the series on the Google event too; `sync_to_calendar`'s patch already omits
  `recurrence` when None, so the calendar side was safe — the DB write was the
  destroyer.
- **`update_metadata_updates()`**: `new_priority`/`new_deadline` written only
  when truthy — an explicit None from a loose construction is "not provided",
  never a wipe.
- **`reschedule`** already wrote only `reminder_at` — PATCH-safe; its Phase 1
  fail-closed else-branch covers the no-time case.
- **Refinement vs. earlier draft:** executor reads stay on the validated
  `params` channel rather than migrating to `action.<field>` attribute access.
  The channel is trustworthy *because* Phase 1 validates at the boundary, and
  attribute access would break legacy/base constructions (UAT builds base
  `Action`s). The loose-dict smell is killed at the boundary, not by
  duplicating access paths in the executor.
- Tests: `tests/unit/test_executor_patch.py` (8 DB-free cases).

### Phase 4 — Stateful clarification — *implemented*

- **`park_action_clarification()`** (workflows.py): `NeedsClarification` in
  dispatch parks the pending action as an `action_clarification` workflow in
  `conversation_workflows` (DB-backed, 7-day expiry, supersedes prior active
  workflows for the thread) — **not Redis, not raw_dumps**.
- **`_resume_action_clarification()`**: the reply is the ANSWER (a date/delta),
  not yes/no. Decline phrases cancel; unrelated replies (no time signal, no
  entity overlap) fall through to normal routing without consuming the
  workflow; otherwise the original text + the answer are re-planned
  (`plan_actions`) and executed, then the workflow resolves atomically. Still
  unclear → re-ask, workflow stays active.
- Dispatch's two `NeedsClarification` handlers park before asking (Phase 4),
  keeping the Phase 1 fail-closed ask.
- **Learning-loop writeback (vision #4):** every clarification resolution
  emits a `subsystem_telemetry` observation (`subsystem="action_planner"`,
  `event_type="clarification"`, features = operation / missing_fields / intent,
  outcome = `confirmed` | `rejected` | `failed`) via the existing
  `emit_observation()` path — persisting AND updating pattern counters so
  Rhodey accumulates how often each operation needs clarification and how the
  user resolves it. Fail-open; exchanges also persist via `log_exchange` +
  audit.
- **Learning-loop consumption (vision #4 — loop closed):** new
  `core/lib/learning_hints.py` reads the `action_planner` pattern counters at
  plan time. `build_planner_hint()` re-ranks patterns by frequency (error-prone
  classes, not confident ones) and maps repeated clarifications to targeted
  MUST-FOLLOW prompt reminders per operation (`reschedule` omitted time,
  `modify_recurring` omitted rrule/time, `update_metadata` omitted
  priority/deadline); `get_action_planner_hint()` wraps it in a 5-min in-process
  TTL cache and is fail-open (a telemetry hiccup never breaks planning).
  `plan_actions()` injects the hint into `build_planner_prompt` as a
  `LEARNED FROM PAST CLARIFICATIONS` section rendered ONLY when non-empty —
  the M9.4 golden stays byte-identical with nothing learned yet. The planner's
  `missing_fields` extraction also now names the REAL parameter
  (`validation_missing_fields()` in models.py: after-validator errors locate
  the op but name the fields in the message — e.g. reschedule →
  `new_reminder_at`/`time_delta` — so both the ask and the learning features
  are precise). E2E proven live on the test tenant: 3 clarification
  observations → reschedule hint materializes → cleanup restores a clean
  slate.
- Tests: `tests/unit/test_workflow_clarification.py` (6 DB/LLM-free cases),
  `tests/unit/test_learning_hints.py` (13 cases: hint translation, frequency
  ranking, threshold/dedup, fail-open cache, prompt section absent-when-empty)
  + `validation_missing_fields` cases in `tests/unit/test_action_models.py`.

- New `workflow_type` (e.g. `action_clarification`) in the **existing**
  `conversation_workflows` machinery — **not Redis** (TTL eviction = lost pending
  action = trust-breaker; the codebase already chose DB here: *"DB lookup is fast
  and restart-safe"*, workflows.py) and **not** `raw_dumps` (a log, not a store).
- Resume branch parses the date/delta reply and re-plans anchored to the pending
  action; superseding + expiry already handled by the machinery.
- Learning-loop writeback: the clarified value trains the planner (vision
  criterion #4 — a clarification that doesn't teach is a missed lesson).

### Phase 5 — Provider-enforced shape — *implemented*

- **`PLANNER_ACTIONS_SCHEMA`** (prompts/planner.py): shape-level — top-level
  `actions` array, per-item `operation` enum (all 12 ops), `params` as object,
  `target_id`/`human_label`/`confidence` loosely typed. **Not** the full
  discriminated union (Gemini `responseSchema` is finicky with `oneOf`);
  Phase 1 typed models remain the strict backstop.
- **Threading:** `config.response_schema` flows through
  `generate_content_with_fallback` → Gemini (passed as `response_schema` to
  `generate_content`) and OpenRouter (`openrouter_response_format()` →
  `response_format: json_schema` with name + non-strict schema).
- **Graceful degradation:** `_schema_rejection()` in providers.py detects
  schema-rejection errors; Gemini retries that client once without the schema
  rather than failing the call (fallback chain covers cross-provider
  degradation).
- The LLM layer's pre-existing post-hoc `schema=` validation (with mutation-
  hint retries) is left untouched — the planner keeps `NeedsClarification` as
  its ask path rather than converting validation failures into silent
  fallbacks.
- Tests: `tests/unit/test_providers_shape.py` (6 DB/network-free cases).

## Non-Goals

- **No classify/plan consolidation** into single tool-calling: the codebase's own
  deterministic pre-filter exists because "LLMs consistently fail" at task-ID
  routing (planner.py), and the classifier carries `possible_intents`,
  `clarification_question`, `receipt` metadata the executor depends on.
- **No `instructor` dependency**: Pydantic v2 is already on the tree via FastAPI.
- **No new layer / new tables**: phases reuse existing homes
  (`conversation_workflows` exists; only a `workflow_type` value is added).
- **Scope:** hard guarantees on the planner path; fail-closed-by-machinery
  elsewhere (QUERY/DELEGATE/commands keep their handlers).

## Verification

- `pytest tests/unit/` — model matrix, no DB required.
- Golden snapshots (`tests/golden/`) regenerated only when prompts change (Ph 2).
- Negative UAT: reschedule without a time must surface a failure, never "✅".
- Live sim (`LIVE_DB=true pytest tests/sim/test_validation_refactor.py`) — E5
  validates `validate_operation` continues to block bad actions.

### Live verification (Aug 13, 2026 — real Supabase + real providers)

Run under the dedicated test tenant (`resolve_test_tenant_uid`), outbound sends
mocked, cross-tenant leak sweep green:

- **UAT (chunked): 23/23 scenarios passed** — incl. **S19** (reschedule updates
  `reminder_at` end-to-end) and **S19b** (reschedule with no time leaves
  `reminder_at` untouched — never a silent ack).
- **Sim suite: 81 passed, 14 skipped** (incl. `test_validation_refactor`).
- **Clusters: 38 passed, 1 xfailed** (incl. `test_workflows` — Phase 4).
- **Phase 5 provider schema (live):** Gemini and OpenRouter both accepted
  `PLANNER_ACTIONS_SCHEMA` (`degraded=False`); Gemini returned clean JSON,
  OpenRouter returned JSON with a trailing code fence that the production
  `parse_json` path strips (verified). No schema rejection → degradation path
  not exercised.
- **Phase 2 end-to-end (live):** "Defer the Ashraya domain purchase by 7 days"
  → `resolve_relative_dates` rewrote to "on August 20, 2026" → `RESOLVED
  _RELATIVE_DATES` in the prompt → real LLM emitted `reschedule`; in the
  run where it dropped the time (`params: {}`), the backstop injected
  `{7, days, later}` and the executor computed `new_reminder_at =
  2026-08-20T12:40:28+05:30`. Measured reliability of the bare prompt across
  variants: 16/16 with a time; the backstop covers the residual flake.

## Post-hardening fix: per-operation acks (no more false "✅ Closed")

Follow-on found during the Aug 20 production check: the executor's success
ack lumped **every** successful mutation into `closed_ids`, so a reschedule
was acknowledged as `✅ Closed: <task>` while the task stayed open — a message-
layer lie (the opposite direction of Aug 12: write happened, words wrong).
The app's message parser maps `✅ Closed:` to a `taskDone` card, so the UI
showed a done-card for an open task.

Fix (`core/actions/executor.py` ack block): group successful actions by
operation and emit one honest line per operation — `✅ Rescheduled: <title> →
<date>` (human-formatted via `_human_friendly_date`), `✅ Recurrence updated:`,
`✅ Updated:`, `🗑️ Deleted event:`, and `✅ Closed:` only for true closures
(`close_task`/`cancel_recurring`/`suppress_instance`). Covered by
`tests/unit/test_executor_acks.py` (6 cases: reschedule with date, DB-title
fallback, closure unchanged, metadata, recurring, mixed batch).

## Ack contract: one verb table, fail-closed rendering (follow-on)

A follow-on audit of every Telegram ack found the same root cause as the
"✅ Closed" bug in three more places, plus an app-side misrender:

1. **"✅ Logged" for tasks/events** — `created_labels` lumped create_task,
   create_note AND create_event into one "Logged" line. A task is on your
   list; an event is scheduled; only notes are logged.
2. **`handle_confident_note` false success** — the except path still sent
   "✅ Captured." after a failed save (the Aug 12 anti-pattern, alive).
3. **"will auto-approve from now on" overclaim** — the suggest-mode approve
   tap wrote `suggest_approved:{subsystem}:{hash}` which was **never read**;
   auto-approval actually keys off pattern stats. One tap changed nothing.
4. **App misrender** — `rich_card_content.dart` mapped any non-Closed ✅ line
   to an *approval card*, so our "✅ Rescheduled" fix showed a bogus approve
   affordance; unknown ✅ lines hit the same generic fallback.

**The design (result-driven acks):**

- `ExecutionResult` (operation, status: committed|failed|skipped|rolled_back,
  target_id, title, values, error) — the executor *emits facts* and never
  writes ack text.
- `render_acks()` in `core/lib/rhodey_voice.py` is the **single verb table**:
  op × status → line. Fail-closed: only `committed` results render, so an
  exception can never produce a success line. Per-op verbs: `📝 Logged`
  (note), `📝 On your list` (task, +date), `📅 Scheduled` (event), `✅
  Rescheduled → date`, `✅ Recurrence updated`, `✅ Updated`, `🗑️ Deleted
  event`, and `✅ Closed` for true closures only.
- **Pattern approve wired** — `compute_pattern_confidence` now reads the
  `suggest_approved` key and overrides to `approve` (rule tagged
  "user-approved"), making the handler's promise true. User judgment beats
  stats.
- **`handle_confident_note`** now sends an honest failure line and returns
  None instead of echoing the success receipt.
- **App contract** — `rich_card_content.dart` parses the same verb table
  (reschedule/update → task card, never taskDone; On your list/Scheduled →
  task; Logged → note), and the generic ✅-approval fallback now requires
  approval language (word "approved" or a ✓/✅ marker) so unknown lines don't
  become approval cards. Pinned by `rhodey_app/test/rich_card_content_test.dart`.

### Rhodey Voice acks + structured intent (the text-parser contract dissolved)

The ack text now follows the voice spec (`core/prompts/voice.py`): "Got it —
X is on your list for Aug 20.", "Moved X to Aug 20.", "Done — X is off your
plate.", "X — logged." — confirmations, contractions, the concrete date delta.
No emoji prefixes: the app no longer parses ack text at all.

- `render_acks()` renders the voice lines; `ACK_INTENTS` maps op → structured
  intent (TASK_CREATED / TASK_RESCHEDULED / TASK_CLOSED / NOTE_LOGGED / …).
- The executor sends the ack with `intent` + `ack_title` (the bare task name)
  → `send_telegram` → `deliver_outbound_reply` → raw_dumps metadata.
- `/api/messages` and `/api/conversation-history` flatten metadata.intent /
  metadata.title onto the row; `ChatMessage.ackTitle` carries it into
  `resolveCardData()`, which renders the card from intent + title — so the
  card title is the bare task name and Mark-Done lookups keep working.
- The text parser remains only as a fallback for legacy (pre-voice) rows.

This kills the parser-drift class permanently: the ack text can change freely
without breaking card rendering, and the card semantics come from the same
structured facts (op/status/title) the backend already trusted.

## Commit Discipline

Every commit carries the 4W1H Root Cause block (enforced by `.githooks/commit-msg`).
The Aug 12 incident is the canonical Root Cause for Phases 1–2:

```
Root Cause: Planner prompt asked the LLM to compute absolute timestamps from
relative deltas with no examples, and the executor acknowledged reschedule
actions with no time instead of failing or asking — a fail-open contract pair.
```
