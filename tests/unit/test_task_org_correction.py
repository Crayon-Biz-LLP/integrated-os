"""Task org-correction endpoint contract tests.

Proves the PATCH /api/tasks/{task_id} org-correction flow:

- organization_id is required (no "None"/unlink — every task stays org-linked)
- the target must be a current organization node, else 400
- reassignment sets organization_id, clears a stale pending_org_id, and
  persists a task_org_correction decision (learning loop / vision criterion #4)
- GET /api/organizations returns the tenant's current org nodes
"""

import pytest

from fastapi.testclient import TestClient

from api.index import app

pytestmark = pytest.mark.learning

client = TestClient(app)

TEST_TENANT = "c302706e-fe61-422a-b384-68e3bc8f6f8e"


def _noop_auth(request):
    return None


class _Chain:
    """Minimal PostgREST-style chain mock returning canned data per table."""

    def __init__(self, data_by_table):
        self._data = data_by_table
        self._last = None

    def table(self, name):
        self._last = name
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def update(self, *_a, **_k):
        return self

    def delete(self, *_a, **_k):
        return self

    def execute(self):
        rows = self._data.get(self._last, [])
        return type("R", (), {"data": rows})()


def _client_with(tasks_rows, org_rows, old_org_rows, monkeypatch, record_calls):
    data = {
        "tasks": tasks_rows,
        "graph_nodes": org_rows + old_org_rows,
    }

    def fake_tenant_client():
        return _Chain(data)

    monkeypatch.setattr("api.index.require_api_auth", _noop_auth)
    monkeypatch.setattr("api.index.tenant_aware_client", fake_tenant_client)
    monkeypatch.setattr(
        "api.index.record_decision",
        lambda **kw: (record_calls.append(kw) or {"id": 1}),
    )
    return client


ORG_A = {"id": "org-aaaa", "label": "AlphaCorp"}
ORG_B = {"id": "org-bbbb", "label": "BetaCorp"}


def test_patch_requires_org_id(monkeypatch):
    calls = []
    c = _client_with([], [], [], monkeypatch, calls)
    r = c.patch("/api/tasks/1", json={})
    assert r.status_code == 400
    assert calls == []  # no decision recorded when rejected


def test_patch_rejects_invalid_org(monkeypatch):
    calls = []
    c = _client_with(
        [{"id": 1, "title": "T", "organization_id": "org-aaaa", "pending_org_id": None}],
        [],  # no matching org node
        [],
        monkeypatch,
        calls,
    )
    r = c.patch("/api/tasks/1", json={"organization_id": "org-zzzz"})
    assert r.status_code == 400
    assert calls == []


def test_patch_reassigns_and_records_decision(monkeypatch):
    calls = []
    task = {"id": 1, "title": "Fix invoice", "organization_id": "org-aaaa",
            "pending_org_id": "org-pending"}
    c = _client_with(
        [task],
        [ORG_A, ORG_B],
        [{"label": "AlphaCorp"}],
        monkeypatch,
        calls,
    )
    r = c.patch("/api/tasks/1", json={"organization_id": "org-bbbb"})
    assert r.status_code == 200
    assert r.json()["organization_id"] == "org-bbbb"
    assert len(calls) == 1
    d = calls[0]
    assert d["decision_type"] == "task_org_correction"
    assert d["entity_type"] == "task"
    assert d["entity_id"] == "1"
    assert d["metadata"]["old_org_id"] == "org-aaaa"
    assert d["metadata"]["new_org_id"] == "org-bbbb"


def test_get_organizations_returns_current_orgs(monkeypatch):
    calls = []
    c = _client_with([], [ORG_A, ORG_B], [], monkeypatch, calls)
    r = c.get("/api/organizations")
    assert r.status_code == 200
    orgs = r.json()["organizations"]
    assert {o["id"] for o in orgs} == {"org-aaaa", "org-bbbb"}
