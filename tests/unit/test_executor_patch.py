"""Phase 3 PATCH-semantics tests (no DB required).

Covers the pure patch builders from `core/actions/executor.py`:
- `modify_recurring_updates` — a time-only change must NEVER write `recurrence`
  (the Phase 3 data-loss class: `upd = {"recurrence": None}` wiped the series).
- `update_metadata_updates` — an explicitly-None field is "not provided", not a wipe.

Run: python -m pytest tests/unit/test_executor_patch.py -v
"""

from core.actions.executor import modify_recurring_updates, update_metadata_updates
from core.actions.models import Action, ModifyRecurringAction, UpdateMetadataAction

RRULE = "RRULE:FREQ=WEEKLY;BYDAY=MO"


def test_modify_recurring_time_only_preserves_recurrence():
    """The Phase 3 data-loss fix: changing the time must not touch recurrence."""
    action = ModifyRecurringAction(
        operation="modify_recurring",
        target_id=5,
        new_reminder_at="2026-08-19T11:00:00+05:30",
    )
    upd = modify_recurring_updates(action)
    assert "reminder_at" in upd
    assert "recurrence" not in upd


def test_modify_recurring_rrule_only():
    action = ModifyRecurringAction(
        operation="modify_recurring", target_id=5, new_rrule=RRULE
    )
    assert modify_recurring_updates(action) == {"recurrence": RRULE}


def test_modify_recurring_both_deltas():
    action = ModifyRecurringAction(
        operation="modify_recurring",
        target_id=5,
        new_rrule=RRULE,
        new_reminder_at="2026-08-19T11:00:00+05:30",
    )
    upd = modify_recurring_updates(action)
    assert set(upd.keys()) == {"recurrence", "reminder_at"}


def test_modify_recurring_legacy_none_params_never_write_recurrence():
    """Legacy base-Action with explicit None in params — no recurrence write."""
    action = Action(
        operation="modify_recurring",
        target_id=5,
        params={"new_rrule": None, "new_reminder_at": "2026-08-19T11:00:00"},
    )
    upd = modify_recurring_updates(action)
    assert "recurrence" not in upd
    assert "reminder_at" in upd


def test_modify_recurring_no_changes_returns_empty():
    action = Action(operation="modify_recurring", target_id=5, params={})
    assert modify_recurring_updates(action) == {}


def test_update_metadata_only_priority():
    action = UpdateMetadataAction(operation="update_metadata", target_id=5, new_priority="high")
    assert update_metadata_updates(action) == {"priority": "high"}


def test_update_metadata_explicit_none_is_not_a_write():
    action = Action(
        operation="update_metadata",
        target_id=5,
        params={"new_priority": None, "new_deadline": "2026-09-01"},
    )
    assert update_metadata_updates(action) == {"deadline": "2026-09-01"}


def test_update_metadata_empty_returns_empty():
    action = Action(operation="update_metadata", target_id=5, params={})
    assert update_metadata_updates(action) == {}
