"""Phase 1 action-model contract tests (no DB required).

Covers the typed action models from `core/actions/models.py`:
discriminated-union validation, per-op required fields (the Aug 12 silent-ack
class), the `params` executor channel (incl. `_created_*` bookkeeping), and
`NeedsClarification` payloads.

Run: python -m pytest tests/unit/test_action_models.py -v
"""



from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.actions.models import (
    Action,
    CloseTaskAction,
    ModifyRecurringAction,
    NeedsClarification,
    PLAN_ACTION_ADAPTER,
    RescheduleAction,
    UpdateMetadataAction,
    action_param_error,
    inject_deterministic_delta,
    validation_missing_fields,
)
pytestmark = pytest.mark.decision



def _validate(raw: dict):
    return PLAN_ACTION_ADAPTER.validate_python(raw)


# ── reschedule: the Aug 12 failure class ──


def test_reschedule_requires_new_reminder_at():
    """A reschedule with no time must fail validation (the Aug 12 case)."""
    with pytest.raises(ValidationError):
        _validate({"operation": "reschedule", "target_id": "2466"})


def test_reschedule_with_time_valid():
    action = _validate({
        "operation": "reschedule",
        "target_id": "2466",
        "params": {"new_reminder_at": "2026-08-19T14:00:00+05:30"},
        "human_label": "Reschedule Ashraya purchase",
    })
    assert isinstance(action, RescheduleAction)
    assert action.new_reminder_at == datetime.fromisoformat("2026-08-19T14:00:00+05:30")
    # Executor channel is in sync
    assert action.params.get("new_reminder_at") == action.new_reminder_at


def test_reschedule_rejects_garbage_date():
    with pytest.raises(ValidationError):
        _validate({
            "operation": "reschedule",
            "params": {"new_reminder_at": "sometime next week"},
        })


def test_reschedule_with_time_delta_only(monkeypatch):
    """Phase 2: the LLM emits the delta; the code computes the timestamp."""
    fixed = datetime(2026, 8, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    monkeypatch.setattr(
        "core.actions.models.resolve_time_delta", lambda d, reference=None: fixed
    )
    action = _validate({
        "operation": "reschedule",
        "target_id": "2466",
        "params": {"time_delta": {"amount": 7, "unit": "days", "direction": "later"}},
    })
    assert isinstance(action, RescheduleAction)
    assert action.new_reminder_at == fixed
    # Executor channel carries the computed absolute time
    assert action.params.get("new_reminder_at") == fixed


def test_reschedule_with_both_prefers_absolute(monkeypatch):
    """An explicit absolute time wins over time_delta (deterministic)."""
    fixed = datetime(2026, 8, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    monkeypatch.setattr(
        "core.actions.models.resolve_time_delta", lambda d, reference=None: fixed
    )
    action = _validate({
        "operation": "reschedule",
        "target_id": "7",
        "params": {
            "new_reminder_at": "2026-08-20T09:00:00+05:30",
            "time_delta": {"amount": 7, "unit": "days"},
        },
    })
    assert action.new_reminder_at == datetime.fromisoformat("2026-08-20T09:00:00+05:30")
    assert action.params.get("new_reminder_at") == action.new_reminder_at


def test_time_delta_must_have_positive_amount():
    with pytest.raises(ValidationError):
        _validate({
            "operation": "reschedule",
            "params": {"time_delta": {"amount": 0, "unit": "days"}},
        })


# ── modify_recurring / update_metadata: at-least-one-of ──


def test_modify_recurring_requires_at_least_one_delta():
    with pytest.raises(ValidationError):
        _validate({"operation": "modify_recurring", "target_id": "5"})

    rrule_only = _validate({
        "operation": "modify_recurring",
        "target_id": "5",
        "params": {"new_rrule": "RRULE:FREQ=WEEKLY"},
    })
    assert isinstance(rrule_only, ModifyRecurringAction)
    assert rrule_only.params.get("new_rrule") == "RRULE:FREQ=WEEKLY"

    time_only = _validate({
        "operation": "modify_recurring",
        "target_id": "5",
        "params": {"new_reminder_at": "2026-08-19T11:00:00"},
    })
    assert isinstance(time_only, ModifyRecurringAction)
    assert time_only.params.get("new_reminder_at") is not None

    delta_only = _validate({
        "operation": "modify_recurring",
        "target_id": "5",
        "params": {"time_delta": {"amount": 2, "unit": "weeks"}},
    })
    assert isinstance(delta_only, ModifyRecurringAction)
    assert delta_only.params.get("new_reminder_at") is not None


def test_update_metadata_requires_at_least_one_delta():
    with pytest.raises(ValidationError):
        _validate({"operation": "update_metadata", "target_id": "5"})

    ok = _validate({
        "operation": "update_metadata",
        "target_id": "5",
        "params": {"new_priority": "high"},
    })
    assert isinstance(ok, UpdateMetadataAction)
    assert ok.params.get("new_priority") == "high"


# ── union discrimination + params passthrough ──


def test_union_discriminates_by_operation():
    assert isinstance(_validate({"operation": "close_task", "target_id": 3}), CloseTaskAction)


def test_create_task_accepts_known_params_and_preserves_unknown():
    action = _validate({
        "operation": "create_task",
        "params": {"title": "Buy milk", "some_future_key": "kept"},
    })
    assert action.params.get("title") == "Buy milk"
    # Unknown keys the LLM sent are never silently dropped
    assert action.params.get("some_future_key") == "kept"


def test_legacy_base_action_params_compat():
    """Legacy `Action(..., params={...})` construction keeps working."""
    legacy = Action(
        operation="create_task",
        params={"title": "Buy milk"},
        human_label="Legacy",
    )
    assert legacy.params.get("title") == "Buy milk"
    # Post-execution bookkeeping writes stick (rollback channel)
    legacy.params["_created_task_id"] = 42
    assert legacy.params.get("_created_task_id") == 42


def test_typed_action_bookkeeping_writes_stick():
    action = RescheduleAction(
        operation="reschedule",
        target_id=7,
        new_reminder_at="2026-08-19T10:00:00",
    )
    action.params["_created_task_id"] = 9
    assert action.params["_created_task_id"] == 9


# ── action_param_error: the executor's DB-free guard ──


def test_action_param_error_reschedule():
    err = action_param_error(Action(operation="reschedule", target_id=1))
    assert err is not None and "new_reminder_at" in err
    ok = RescheduleAction(operation="reschedule", target_id=1, new_reminder_at="2026-08-19T10:00:00")
    assert action_param_error(ok) is None


def test_action_param_error_modify_recurring_and_metadata():
    err = action_param_error(Action(operation="modify_recurring", target_id=1))
    assert err is not None and "new_rrule" in err
    err2 = action_param_error(Action(operation="update_metadata", target_id=1))
    assert err2 is not None and "new_priority" in err2
    assert action_param_error(Action(operation="close_task", target_id=1)) is None


# ── NeedsClarification payload ──


def test_needs_clarification_payload():
    nc = NeedsClarification(
        "invalid action",
        text="defer the ashraya purchase by 7 days",
        operation="reschedule",
        target_id="2466",
        missing_fields=["new_reminder_at"],
    )
    assert nc.operation == "reschedule"
    assert nc.target_id == "2466"


# ── inject_deterministic_delta: Phase 2 backstop for the LLM time flake ──


def test_inject_delta_when_llm_dropped_time():
    """Live verification caught the LLM emitting reschedule with params: {} —
    the backstop re-reads the raw text and injects the delta, so the action
    validates and executes instead of being asked about or dropped."""
    a = {"operation": "reschedule", "target_id": "2466", "params": {},
         "human_label": "Defer the Ashraya domain purchase by 7 days"}
    out = inject_deterministic_delta(a, "Defer the Ashraya domain purchase by 7 days")
    assert out["params"]["time_delta"] == {"amount": 7, "unit": "days", "direction": "later"}
    act = _validate(out)  # the injected action now passes the typed contract
    assert act.params["new_reminder_at"]  # resolved to an absolute time


def test_inject_keeps_explicit_absolute_time():
    a = {"operation": "reschedule", "target_id": "2466",
         "params": {"new_reminder_at": "2026-08-19T14:00:00+05:30"}}
    out = inject_deterministic_delta(a, "Defer the Ashraya domain purchase by 7 days")
    assert out["params"] == {"new_reminder_at": "2026-08-19T14:00:00+05:30"}


def test_inject_keeps_explicit_delta():
    a = {"operation": "reschedule", "target_id": "2466",
         "params": {"time_delta": {"amount": 2, "unit": "weeks"}}}
    out = inject_deterministic_delta(a, "Defer the Ashraya domain purchase by 7 days")
    assert out["params"]["time_delta"] == {"amount": 2, "unit": "weeks"}


def test_inject_ignores_non_time_ops():
    a = {"operation": "close_task", "target_id": "5", "params": {}}
    assert inject_deterministic_delta(a, "by 7 days") is a


def test_inject_leaves_action_unchanged_when_text_has_no_delta():
    """No computable delta → unchanged → the typed contract's fail-closed
    path (NeedsClarification) still applies. Never a silent ack."""
    a = {"operation": "reschedule", "target_id": "2466", "params": {}}
    out = inject_deterministic_delta(a, "reschedule the purchase")
    assert out is a


def test_inject_modify_recurring():
    a = {"operation": "modify_recurring", "target_id": "2466", "params": {}}
    out = inject_deterministic_delta(a, "push the weekly sync back a week")
    assert out["params"]["time_delta"] == {"amount": 1, "unit": "weeks", "direction": "later"}


# ── validation_missing_fields: real-field extraction for the learning loop ──


def test_validation_missing_fields_names_real_field_on_real_error():
    """The Aug 12 shape (reschedule, no time) yields a loc of ('reschedule',)
    only — the extraction must surface new_reminder_at/time_delta so the
    learning loop and clarification ask name the actual missing parameter."""
    with pytest.raises(ValidationError) as exc:
        _validate({"operation": "reschedule", "target_id": "2466"})
    missing = validation_missing_fields(exc.value.errors())
    assert missing, "expected at least one missing-field entry"
    joined = missing[0]
    assert "new_reminder_at" in joined
    assert "time_delta" in joined


def test_validation_missing_fields_unknown_errors_kept_verbatim():
    # A field-level error locates the field directly and is preserved as-is.
    missing = validation_missing_fields([{"loc": ("params", "new_deadline"), "msg": "bad date"}])
    assert missing == ["params.new_deadline"]
