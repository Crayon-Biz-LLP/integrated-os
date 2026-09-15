"""Typed action models for the planner → executor contract.

Phase 1 of action-pipeline hardening (see `product-summary/61-action-pipeline-hardening.md`):
per-operation Pydantic models with required fields, discriminated by `operation`.
A malformed LLM plan is rejected at the plan boundary (`NeedsClarification`)
instead of being silently acknowledged by the executor.

Backward compatibility:
- `Action(operation=..., params={...})` legacy construction still works — `params`
  remains a real dict field that the executor reads via `.get(...)` and writes
  `_created_*` rollback bookkeeping into.
- Typed per-op models (constructed from LLM JSON via the `PlannedAction` union)
  validate required fields at construction and keep `params` in sync with their
  typed fields, so the executor is unchanged.
"""

import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from core.lib.time_utils import derive_due_fields, now_for_user

from core.lib.time_utils import extract_time_delta, resolve_time_delta

# Fields that are part of the action envelope, not per-op parameters.
_ENVELOPE_FIELDS = {
    "operation",
    "target_id",
    "confidence",
    "human_label",
    "organization_id",
    "params",
}


class ExtractionDegraded(RuntimeError):
    """Raised when the extraction LLM itself failed (all providers down / safe
    hold) — Sep 14 L2 contract: an infrastructure failure is NOT an unclear
    answer, so it must never be conflated with NeedsClarification.

    Callers must keep pending state alive and tell the user the truth ("my
    side had a problem, try again"), never cancel a parked decision and never
    blame the user's wording.
    """


class NeedsClarification(Exception):
    """Raised by `extract_suggestions()` when the LLM plan fails schema validation.

    Carries enough context for the dispatch layer to ask the user a precise
    follow-up question instead of silently dropping or acknowledging the action.
    Phase 4 upgrades this into the stateful clarification workflow.
    """

    def __init__(
        self,
        message: str,
        text: str = "",
        operation: Optional[str] = None,
        target_id=None,
        missing_fields: Optional[list] = None,
    ):
        self.message = message
        self.text = text
        self.operation = operation
        self.target_id = target_id
        self.missing_fields = missing_fields or []
        super().__init__(message)

    def to_question(self) -> str:
        """A user-facing clarification question."""
        if self.operation == "reschedule":
            return (
                "I know you want to reschedule this, but I didn't catch the new "
                "date or time. When should I move it to?"
            )
        if self.operation == "modify_recurring":
            return (
                "I know you want to change the schedule, but I didn't get the new "
                "time. What should the new time be?"
            )
        if self.operation == "update_metadata":
            return (
                "I know you want to update this task, but I didn't catch what to "
                "change. What should I update?"
            )
        missing = ", ".join(self.missing_fields) or "some details"
        return f"I didn't catch {missing}. Can you rephrase that?"


class Action(BaseModel):
    """Base action. Per-op subclasses add typed fields and strict validation.

    `params` is the executor's read/write channel: the LLM nests operation
    parameters under it, the typed models keep it in sync with validated
    fields, and post-execution `_created_*` bookkeeping lands in it for
    rollback (`compensate_action`).
    """

    operation: str
    target_id: Optional[Union[int, str]] = None
    confidence: float = 1.0
    human_label: str = ""
    organization_id: Optional[str] = None
    params: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _promote_known_params(cls, data: Any) -> Any:
        """Promote known op params from the `params` dict into typed fields.

        The LLM nests operation parameters under `params`; the schema declares
        them as top-level fields so required-field validation applies. Unknown
        keys stay inside `params` and are never dropped.
        """
        if isinstance(data, dict):
            params = data.get("params")
            if isinstance(params, dict):
                for key, value in params.items():
                    if key in cls.model_fields:
                        data.setdefault(key, value)
        return data

    @model_validator(mode="after")
    def _sync_params(self) -> "Action":
        """Keep the executor's `params` channel in sync with typed fields.

        Typed (validated) values win; unknown keys the LLM sent are preserved.
        """
        for name in type(self).model_fields:
            if name in _ENVELOPE_FIELDS:
                continue
            value = getattr(self, name)
            if value is not None:
                self.params[name] = value
        return self


# ── Per-operation models ──


class CloseTaskAction(Action):
    """Marks a normal Task as done."""

    operation: Literal["close_task"]


class SuppressInstanceAction(Action):
    """Skips the next occurrence of a recurring Task."""

    operation: Literal["suppress_instance"]


class CancelRecurringAction(Action):
    """Ends a recurring Task series entirely."""

    operation: Literal["cancel_recurring"]


class DeleteEventAction(Action):
    """Removes an external Calendar event."""

    operation: Literal["delete_event"]


class QueryInfoAction(Action):
    """Fetches information from the brain (informational only)."""

    operation: Literal["query_info"]
    query: Optional[str] = None


class NoOpAction(Action):
    """Nothing matched — no-op."""

    operation: Literal["no_op"]


class TimeDelta(BaseModel):
    """Structured relative-time delta extracted by the LLM.

    The LLM reads the phrasing ("defer by 7 days") into this structure; the
    system computes the absolute timestamp — LLMs are unreliable at calendar
    math (the Aug 12 silent-ack failure). Invariant #2.
    """

    amount: int = Field(gt=0, description="Number of units")
    unit: Literal["days", "weeks", "hours"] = "days"
    direction: Literal["later", "earlier"] = "later"


class RescheduleAction(Action):
    """Changes the time of a non-recurring Task.

    Requires `new_reminder_at` (absolute ISO datetime) OR `time_delta` (the
    code computes the timestamp). A reschedule with neither was the Aug 12
    silent-ack failure — it now raises a ValidationError → NeedsClarification.
    """

    operation: Literal["reschedule"]
    new_reminder_at: Optional[datetime] = None
    time_delta: Optional[TimeDelta] = None

    @model_validator(mode="after")
    def _resolve_time(self) -> "RescheduleAction":
        if self.new_reminder_at is None:
            if self.time_delta is not None:
                # Invariant #2: the code computes the timestamp.
                self.new_reminder_at = resolve_time_delta(self.time_delta.model_dump())
                self.params["new_reminder_at"] = self.new_reminder_at
            else:
                raise ValueError("reschedule requires new_reminder_at or time_delta")
        return self


class ModifyRecurringAction(Action):
    """Changes the schedule of a recurring Task (at least one delta required)."""

    operation: Literal["modify_recurring"]
    new_rrule: Optional[str] = None
    new_reminder_at: Optional[datetime] = None
    time_delta: Optional[TimeDelta] = None

    @model_validator(mode="after")
    def _resolve_time(self) -> "ModifyRecurringAction":
        if self.new_reminder_at is None and self.time_delta is not None:
            # Invariant #2: the code computes the timestamp.
            self.new_reminder_at = resolve_time_delta(self.time_delta.model_dump())
            self.params["new_reminder_at"] = self.new_reminder_at
        return self

    @model_validator(mode="after")
    def _require_delta(self) -> "ModifyRecurringAction":
        if self.new_rrule is None and self.new_reminder_at is None:
            raise ValueError(
                "modify_recurring requires at least one of new_rrule, new_reminder_at or time_delta"
            )
        return self


class UpdateMetadataAction(Action):
    """Changes priority or deadline of a Task (at least one delta required)."""

    operation: Literal["update_metadata"]
    new_priority: Optional[str] = None
    new_deadline: Optional[str] = None

    @model_validator(mode="after")
    def _require_delta(self) -> "UpdateMetadataAction":
        if self.new_priority is None and self.new_deadline is None:
            raise ValueError(
                "update_metadata requires at least one of new_priority or new_deadline"
            )
        return self


class CreateTaskAction(Action):
    """Creates a new Task."""

    operation: Literal["create_task"]
    title: Optional[str] = None
    organization_name: Optional[str] = None
    deadline: Optional[str] = None
    reminder_at: Optional[str] = None
    priority: Optional[str] = None
    duration_mins: Optional[int] = None
    recurrence: Optional[str] = None
    rrule: Optional[str] = None
    direction: Optional[str] = None
    committed_to: Optional[str] = None


class CreateNoteAction(Action):
    """Saves information to memory."""

    operation: Literal["create_note"]
    content: Optional[str] = None
    organization_name: Optional[str] = None


class CreateEventAction(Action):
    """Schedules a Calendar event."""

    operation: Literal["create_event"]
    title: Optional[str] = None
    time: Optional[str] = None
    reminder_at: Optional[str] = None
    duration_mins: Optional[int] = None
    organization_name: Optional[str] = None


# ── Discriminated union + validator ──

PlannedAction = Annotated[
    Union[
        CloseTaskAction,
        SuppressInstanceAction,
        CancelRecurringAction,
        ModifyRecurringAction,
        RescheduleAction,
        UpdateMetadataAction,
        CreateTaskAction,
        CreateNoteAction,
        CreateEventAction,
        QueryInfoAction,
        DeleteEventAction,
        NoOpAction,
    ],
    Field(discriminator="operation"),
]

PLAN_ACTION_ADAPTER = TypeAdapter(PlannedAction)


def inject_deterministic_title(action: dict, title: str, text: str) -> dict:
    """Phase 2 backstop (invariant #2): if the LLM produced a create_task or
    create_event action with NO title (the S2 flake class — the Gemini planner
    intermittently omits the title, and a title-less create is blocked at the
    executor gate, silently degrading the request to a fallback note), re-read
    the raw text deterministically and inject the title before validation.

    Prefers the classifier-extracted title, falling back to the raw message
    text. When the action carries a non-empty title (or is not a create op)
    the action is returned unchanged.
    """
    op = action.get("operation")
    if op not in ("create_task", "create_event"):
        return action
    params = action.get("params") or {}
    if (params.get("title") or "").strip():
        return action
    params = dict(params)
    params["title"] = (title or "").strip() or text
    action = dict(action)
    action["params"] = params
    return action


# Sep 14 fix #1 (Mode A): the planner returned `reschedule` with EMPTY params
# for date-only requests — reproducibly — and every backstop downstream needs
# SOMETHING to work with. This map lets deterministic code own the date
# arithmetic for the small closed set of day-words English actually uses.
_DATE_WORD_DAYS = {
    "today": 0,
    "tonight": 0,
    "tomorrow": 1,
}


def inject_deterministic_date_words(action: dict, text: str, current_reminder_at=None) -> dict:
    """Sep 14 fix #1: date-word backstop for date-only reschedules.

    The planner's prompt rule ("date without clock time → deadline, null
    reminder_at") is right for CREATION but structurally invalid for
    RESCHEDULE (validation requires a time), and when the planner returns
    `params: {}` outright there is no date for the preserve-time injector to
    work with — the action dies at validation and the user gets asked for a
    time their task already has (live repro, Sep 14: 'Move my rental
    agreement signing task to tomorrow' → input {'params': {}}).

    Deterministic recovery, in the codebase's own invariant #2 spirit ("the
    LLM reads the phrasing, the code does the arithmetic"): if the raw text
    carries an unambiguous day-word (today/tomorrow/tonight), the code injects
    the DATE itself and keeps the task's CURRENT clock time (via the
    preserve-time injector's anchoring) — the planner only had to say "move
    it", not do calendar math. Never fires when the planner already produced a
    time or delta; never invents a time when the task has none (fail-closed
    clarification still applies); unknown day-phrasings are left to the LLM.

    Args:
        action: raw planner action dict (pre-validation).
        text: the raw user text (day-words are read from here, not the LLM).
        current_reminder_at: the target task's current reminder (ISO str or
            None). When None, only the date is injected and the preserve-time
            step decides whether a time can be anchored.
    """
    if action.get("operation") != "reschedule":
        return action
    params = action.get("params") or {}
    if params.get("new_reminder_at") or params.get("time_delta"):
        return action  # planner produced a usable answer — nothing to rescue
    match = re.search(r"\b(today|tonight|tomorrow)\b", (text or "").lower())
    if not match:
        return action  # no unambiguous day-word — leave to the LLM/clarification
    try:
        from core.lib.time_utils import now_for_user
        base_date = now_for_user().date() + timedelta(days=_DATE_WORD_DAYS[match.group(1)])
    except Exception:
        return action  # can't anchor "now" deterministically — fail closed

    params = dict(params)
    current_dt = None
    if current_reminder_at:
        try:
            current_dt = datetime.fromisoformat(str(current_reminder_at).replace("Z", "+00:00"))
        except Exception:
            current_dt = None
    if current_dt is not None:
        # Date from the day-word + the task's own clock time, in the task's
        # existing tz offset. Anchored to the task's reminder, never now().
        preserved = current_dt.replace(
            year=base_date.year, month=base_date.month, day=base_date.day
        )
        params["new_reminder_at"] = preserved.isoformat()
    else:
        # No current time to preserve: inject the date as the deadline so the
        # preserve-time injector (or validation) decides the rest — at least
        # the planner's empty-params hole is filled with a real date.
        params["deadline"] = base_date.isoformat()
    action = dict(action)
    action["params"] = params
    return action


def inject_deterministic_preserve_time(action: dict, current_reminder_at=None) -> dict:
    """Sep 14 backstop for the date-without-clock-time reschedule ("move my
    task to tomorrow"). The planner prompt's "date-only → deadline, null
    reminder_at" rule is right for CREATION but wrong for RESCHEDULE: the
    RescheduleAction contract then fails validation → NeedsClarification,
    asking the user for a time they already have (the task's current one).

    Deterministic recovery: keep the task's existing clock time, move the
    date. Never invents a time when the task has none — the fail-closed
    clarification path still applies there (truly no time to preserve).
    """
    if action.get("operation") != "reschedule":
        return action
    params = action.get("params") or {}
    if params.get("new_reminder_at") or params.get("time_delta"):
        return action  # explicit time or computable delta — nothing to preserve
    deadline = params.get("deadline")
    if not deadline:
        return action  # no date at all — legit NeedsClarification
    # Anchor to the task's CURRENT reminder, not now(): if processing is
    # delayed past the slot (or runs the next day), the preserved clock time
    # must still come from the task, not from when the message was handled.
    if not current_reminder_at:
        return action
    try:
        from datetime import datetime as _dt
        current_dt = _dt.fromisoformat(str(current_reminder_at).replace('Z', '+00:00'))
        deadline_date = str(deadline)[:10]
        preserved = current_dt.replace(year=int(deadline_date[:4]),
                                       month=int(deadline_date[5:7]),
                                       day=int(deadline_date[8:10]))
        params = dict(params)
        params["new_reminder_at"] = preserved.isoformat()
        params.pop("deadline", None)
        action = dict(action)
        action["params"] = params
        return action
    except Exception:
        # Malformed deadline/current reminder — fail closed (clarification),
        # never guess.
        return action


def inject_deterministic_delta(action: dict, text: str) -> dict:
    """Phase 2 backstop (invariant #2): if the LLM produced a time-bearing
    action with NO time (the Aug 12 silent-ack class — live LLM flake seen in
    verification: reschedule with `params: {}`), re-read the raw text
    deterministically and inject the extracted delta before validation.

    The LLM reads the phrasing; the code does the arithmetic. When the text
    carries no computable delta the action is returned unchanged, and the
    typed contract's fail-closed path (NeedsClarification) applies instead —
    never a silent ack, never a needless ask for info already given.
    """
    op = action.get("operation")
    if op not in ("reschedule", "modify_recurring"):
        return action
    params = action.get("params") or {}
    if params.get("new_reminder_at") or params.get("time_delta"):
        return action
    delta = extract_time_delta(text)
    if not delta:
        return action
    params = dict(params)
    params["time_delta"] = delta
    action = dict(action)
    action["params"] = params
    return action


def inject_deterministic_due(action: dict, text: str) -> dict:
    """Deterministic backstop (invariant #2) for the CREATION case: if the LLM
    produced a create_task action with NO due fields while the raw text carries
    an explicit clock time and/or date phrase ("Remind me to call Gopi tomorrow
    at 11AM"), re-read the text deterministically and inject the due fields
    before validation.

    Born from the Sep 10 Gopi regression: the planner prompt's "tomorrow →
    deadline, return null for reminder_at" rule also suppressed "tomorrow at
    11AM", so a time-bearing reminder was created dateless and never synced to
    Google Calendar. Uses derive_due_fields() — code does the arithmetic, never
    the LLM. Ambiguous clock references (bare "at 11" without am/pm) are NOT
    invented; when no date phrase exists the action is returned unchanged.

    Mirrors inject_deterministic_title / inject_deterministic_delta: silent
    data loss is replaced by deterministic recovery at the same injection site.
    """
    if action.get("operation") != "create_task":
        return action
    params = action.get("params") or {}
    if params.get("reminder_at") or params.get("deadline"):
        return action
    reminder_at, deadline, duration = derive_due_fields(text, now_for_user())
    if not deadline:
        return action
    params = dict(params)
    if reminder_at:
        params["reminder_at"] = reminder_at
    if duration:
        params["duration_mins"] = duration
    params["deadline"] = deadline
    action = dict(action)
    action["params"] = params
    return action


_PARAM_FIELD_NAMES = ("new_reminder_at", "time_delta", "new_rrule",
                      "new_priority", "new_deadline")


def validation_missing_fields(errors: list) -> list[str]:
    """Extract missing-field names from Pydantic validation errors.

    After-validator failures (e.g. reschedule with no time) locate the whole
    op (``('reschedule',)``) but name the real fields in the message — the op
    alone is low-signal for the learning loop and the clarification ask.
    Each entry is the dotted loc joined with the fields named in the message.
    """
    missing: list = []
    for err in errors:
        loc = err.get("loc", ())
        parts = [str(p) for p in loc] if loc else []
        msg = err.get("msg", "")
        for f in _PARAM_FIELD_NAMES:
            if f in msg and f not in parts:
                parts.append(f)
        if parts:
            missing.append(".".join(parts))
    return missing


def deadline_rollover(current_deadline, new_reminder_at) -> Optional[str]:
    """Keep a date-only `deadline` honest when a reschedule crosses it.

    A stale-in-the-past date-only deadline (set when the task was created for
    "today") must roll to the new reminder's date when the reminder moves past
    it; a deadline still in the future is a real commitment and stays put.
    Time-bearing deadlines are never touched. Returns the ISO date string for
    the patch, or None when no rollover applies (PATCH semantics — no write).
    """
    try:
        if not current_deadline or not new_reminder_at:
            return None
        from datetime import timezone as _tz
        # A time-bearing deadline is a precise commitment ("by 9:00"), not a
        # day-level fact — never auto-moved; only date-only deadlines roll.
        if "T" in str(current_deadline):
            return None
        # deadline is date-only ('2026-09-14'); compare as a date.
        d = datetime.strptime(str(current_deadline)[:10], "%Y-%m-%d").date()
        r = datetime.fromisoformat(str(new_reminder_at).replace("Z", "+00:00"))
        if r.tzinfo is None:
            r = r.replace(tzinfo=_tz.utc)
        # Anchor to the *tenant's* calendar day, not UTC (11:30 UTC = 5pm IST
        # is still Sep 15 in IST, but a 19:30 UTC reminder is Sep 16 in IST).
        from core.lib.time_utils import get_user_timezone
        r_date = r.astimezone(get_user_timezone()).date()
        if r_date > d:
            return r_date.isoformat()
    except Exception:
        return None
    return None


def action_param_error(action: "Action") -> Optional[str]:
    """Fail-closed check for per-op required parameters (pure schema, no DB).

    Mirror of the per-op model validators, for actions constructed loosely
    (base `Action` / legacy paths) that bypass typed-model construction.
    Called by the executor's `validate_operation` before any DB access.
    """
    op = action.operation
    if op == "reschedule":
        if not action.params.get("new_reminder_at"):
            # Executor only acts on an absolute time. `time_delta` is resolved
            # to an absolute time at the typed-model boundary; loosely-built
            # actions must carry the absolute time themselves.
            return "reschedule: missing new_reminder_at (no time provided — provide an absolute time or time_delta)"
    if op == "modify_recurring":
        if not action.params.get("new_rrule") and not action.params.get("new_reminder_at"):
            return "modify_recurring: requires at least one of new_rrule or new_reminder_at"
    if op == "update_metadata":
        if not action.params.get("new_priority") and not action.params.get("new_deadline"):
            return "update_metadata: requires at least one of new_priority or new_deadline"
    return None
