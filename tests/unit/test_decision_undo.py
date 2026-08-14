"""Unit tests for the per-item undo safety net (Layer 1+2).

Covers:
- `build_action_ledger`: only committed executor results enter the ledger,
  and each entry carries the id needed to reverse it.
- `/api/decisions/undo`: reverses the decision record, re-pends the
  underlying message, and walks the ledger backwards to compensate side
  effects — plus the fail-closed guards (already-decided, missing id).

This is the accidental-tap protection that the old auto-decisions undo never
covered: manual approve/reject (auto_decided=False) now carries the same
recovery path.
"""
from collections import defaultdict
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.index import app
from core.webhook.utils import build_action_ledger

client = TestClient(app)


# ── Fake supabase: chainable, records update payloads per table ─────────

class _Chain:
    def __init__(self, table_name, owner):
        self.table_name = table_name
        self.owner = owner

    def __getattr__(self, name):
        def _f(*args, **kwargs):
            return self
        return _f

    def update(self, payload):
        self.owner.updates[self.table_name].append(payload)
        return self

    def execute(self):
        return SimpleNamespace(data=self.owner.data.get(self.table_name))


class _FakeSupabase:
    def __init__(self, data=None):
        self.data = data or {}
        self.updates = defaultdict(list)

    def table(self, name):
        return _Chain(name, self)


def _decision(**overrides) -> dict:
    decision = {
        "id": 2256,
        "decision_type": "channel_approval",
        "title": "Confirm if issue is sorted",
        "entity_type": "message",
        "entity_id": "7353",
        "status": "active",
        "verified_at": None,
        "reversible": True,
        "metadata": None,
    }
    decision.update(overrides)
    return decision


def _noop_auth(request):
    return None


# ── build_action_ledger ────────────────────────────────────────────────

def _result(operation, target_id=None, title="", status="committed"):
    return SimpleNamespace(operation=operation, target_id=target_id,
                           title=title, status=status)


def test_ledger_includes_only_committed_actions():
    ledger = build_action_ledger([
        _result("close_task", 3167, "Close Ashraya"),
        _result("create_task", 9001, "New task"),
        _result("create_task", None, "blocked", status="failed"),
        _result("close_task", 12, "rolled back", status="rolled_back"),
        _result("query_info", None, "info", status="skipped"),
    ])
    assert ledger == [
        {"operation": "close_task", "target_id": "3167", "title": "Close Ashraya"},
        {"operation": "create_task", "target_id": "9001", "title": "New task"},
    ]


def test_ledger_empty_when_nothing_committed():
    assert build_action_ledger([]) == []
    assert build_action_ledger([_result("no_op", status="skipped")]) == []


def test_ledger_handles_none_results():
    assert build_action_ledger(None) == []


# ── /api/decisions/undo ────────────────────────────────────────────────

def test_undo_reverses_decision_message_and_side_effects(monkeypatch):
    """The full accidental-tap recovery: decision reversed, message re-pended,
    and the ledger walked backwards (create undone before its closer)."""
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    fake = _FakeSupabase({
        "decisions": [_decision(metadata={"actions": [
            {"operation": "close_task", "target_id": "3167", "title": "Close Ashraya"},
            {"operation": "create_task", "target_id": "9001", "title": "New task"},
        ]})],
        "messages": [],
    })
    monkeypatch.setattr("api.index.tenant_aware_client", lambda: fake)

    reversed_calls = []
    monkeypatch.setattr(
        "core.decisions.reverse_decision",
        lambda decision_id, rationale=None: reversed_calls.append((decision_id, rationale)) or True,
    )

    compensated = []
    async def _compensate(action, supabase):
        compensated.append(action)

    monkeypatch.setattr("core.actions.executor.compensate_action", _compensate)

    r = client.post("/api/decisions/undo", json={"decision_id": 2256})
    body = r.json()
    assert r.status_code == 200
    assert body["success"] is True
    assert body["decision_id"] == 2256
    assert body["reverted"] == 1
    assert len(body["actions_reversed"]) == 2

    # Decision record reversed
    assert reversed_calls == [(2256, "User undid manual decision via app undo")]

    # Message re-pended (danny_decision + decided_at cleared)
    assert fake.updates["messages"] == [
        {"danny_decision": None, "decided_at": None},
    ]

    # Side effects compensated in REVERSE order, with ids reconstructed so
    # compensate_action can reverse them (created id for creates, target id
    # for closures).
    assert [c.operation for c in compensated] == ["create_task", "close_task"]
    assert compensated[0].params.get("_created_task_id") == 9001
    assert compensated[1].target_id == 3167


def test_undo_refuses_already_decided(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    fake = _FakeSupabase({
        "decisions": [_decision(status="reversed")],
        "messages": [],
    })
    monkeypatch.setattr("api.index.tenant_aware_client", lambda: fake)
    reversed_calls = []
    monkeypatch.setattr(
        "core.decisions.reverse_decision",
        lambda decision_id, rationale=None: reversed_calls.append(decision_id) or True,
    )
    compensated = []
    async def _compensate(action, supabase):
        compensated.append(action)
    monkeypatch.setattr("core.actions.executor.compensate_action", _compensate)

    r = client.post("/api/decisions/undo", json={"decision_id": 2256})
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert "already" in r.json()["message"]
    # Nothing reversed, nothing compensated
    assert reversed_calls == []
    assert compensated == []
    assert fake.updates["messages"] == []


def test_undo_requires_decision_id(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    r = client.post("/api/decisions/undo", json={})
    assert r.status_code == 400


def test_undo_unknown_decision(monkeypatch):
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    fake = _FakeSupabase({"decisions": [], "messages": []})
    monkeypatch.setattr("api.index.tenant_aware_client", lambda: fake)
    r = client.post("/api/decisions/undo", json={"decision_id": 99999})
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert "not found" in r.json()["message"]


def test_undo_without_ledger_still_repends(monkeypatch):
    """A decision with no executed actions (e.g. a blocked plan) still gets
    its decision reversed and the message re-pended — the message row revert
    is not gated on having side effects to reverse."""
    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    fake = _FakeSupabase({
        "decisions": [_decision()],  # metadata None → no ledger
        "messages": [],
    })
    monkeypatch.setattr("api.index.tenant_aware_client", lambda: fake)
    monkeypatch.setattr("core.decisions.reverse_decision", lambda decision_id, rationale=None: True)
    compensated = []
    async def _compensate(action, supabase):
        compensated.append(action)
    monkeypatch.setattr("core.actions.executor.compensate_action", _compensate)

    r = client.post("/api/decisions/undo", json={"decision_id": 2256})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["reverted"] == 1
    assert r.json()["actions_reversed"] == []
    assert fake.updates["messages"] == [{"danny_decision": None, "decided_at": None}]
    assert compensated == []
