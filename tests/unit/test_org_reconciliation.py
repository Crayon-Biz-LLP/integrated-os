"""Regression tests for org reconciliation on the direct-execution path.

Pins the Aug-26 Finding A/B fixes:
  - Finding A: direct-path tasks must inherit the extracted context's org
    (live or pending) when the action carries none — previously the handler
    only consulted pending_org_id, so tasks mentioning an EXISTING org
    landed with organization_id=NULL.
  - Finding B: when extraction finds NO org, planner-fabricated org
    references are stripped — previously the planner could invent
    "Meeting with Nordlicht" from the org-ID list for a message naming
    no organization.
  - Explicit per-action orgs that differ from ctx's are preserved.

Marker: ingest (message-processing pipeline logic).
"""

import pytest

from core.actions.executor import reconcile_action_orgs

pytestmark = [pytest.mark.ingest]


class _Ctx:
    """Minimal EntityContext stand-in (only the two org fields matter)."""

    def __init__(self, organization_id=None, pending_org_id=None):
        self.organization_id = organization_id
        self.pending_org_id = pending_org_id


class _Action:
    """Minimal Action stand-in (operation + params + optional attr)."""

    def __init__(self, operation, params=None, organization_id=None):
        self.operation = operation
        self.params = dict(params or {})
        if organization_id is not None:
            self.organization_id = organization_id


# ── Finding A: ctx org fills action gaps ──────────────────────────────

def test_fill_live_org_from_ctx():
    actions = [_Action("create_task", {"title": "Review accounts"})]
    reconcile_action_orgs(actions, _Ctx(organization_id="org-1"))
    assert actions[0].params["organization_id"] == "org-1"
    assert actions[0].organization_id == "org-1"


def test_fill_pending_org_from_ctx_when_no_live():
    actions = [_Action("create_note", {"content": "met the team"})]
    reconcile_action_orgs(actions, _Ctx(pending_org_id="pending-9"))
    assert actions[0].params["organization_id"] == "pending-9"


def test_fill_applies_to_all_creation_ops():
    actions = [
        _Action("create_task", {"title": "t"}),
        _Action("create_note", {"content": "n"}),
        _NonCreation(),
    ]
    reconcile_action_orgs(actions, _Ctx(organization_id="org-2"))
    assert actions[0].params["organization_id"] == "org-2"
    assert actions[1].params["organization_id"] == "org-2"


class _NonCreation:
    operation = "close_task"
    params = {}


def test_non_creation_actions_untouched():
    action = _NonCreation()
    reconcile_action_orgs([action], _Ctx(organization_id="org-3"))
    assert "organization_id" not in action.params


# ── Finding B: fabricated orgs stripped when ctx has none ────────────

def test_strip_fabricated_params_org_when_ctx_empty():
    action = _Action(
        "create_task",
        {"title": "Meeting with Nordlicht", "organization_name": "Nordlicht"},
        organization_id="fabricated-id",
    )
    reconcile_action_orgs([action], _Ctx())  # extraction found nothing
    assert "organization_id" not in action.params
    assert "organization_name" not in action.params
    assert getattr(action, "organization_id", None) is None


def test_title_survives_the_strip():
    """Only the fabricated LINK is removed — user content is never touched."""
    action = _Action(
        "create_event",
        {"title": "Meeting with Nordlicht", "time": "2026-08-27T16:00:00+05:30"},
    )
    reconcile_action_orgs([action], _Ctx())
    assert action.params["title"] == "Meeting with Nordlicht"
    assert action.params["time"] == "2026-08-27T16:00:00+05:30"


# ── Non-interference ─────────────────────────────────────────────────

def test_explicit_action_org_differs_from_ctx_is_preserved():
    action = _Action("create_task", {"title": "x", "organization_id": "other-org"})
    reconcile_action_orgs([action], _Ctx(organization_id="ctx-org"))
    assert action.params["organization_id"] == "other-org"


def test_no_ctx_org_and_no_action_org_is_a_noop():
    action = _Action("create_task", {"title": "plain"})
    reconcile_action_orgs([action], _Ctx())
    assert "organization_id" not in action.params


def test_none_actions_list_is_safe():
    reconcile_action_orgs(None, _Ctx(organization_id="org-x"))  # must not raise


def test_missing_params_dict_gets_created():
    class _NoParams:
        operation = "create_task"

    action = _NoParams()
    reconcile_action_orgs([action], _Ctx(organization_id="org-4"))
    assert action.params["organization_id"] == "org-4"
