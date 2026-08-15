import sys
from unittest import mock

from fastapi.testclient import TestClient
from api.index import app
import pytest
pytestmark = pytest.mark.pulse


client = TestClient(app)

def test_health_check():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "Integrated OS API is running on Python 🐍"}


def test_pulse_cron_spawns_workers_with_keyword_args(monkeypatch):
    calls = []

    class FakeSpawn:
        async def aio(self, **kwargs):
            calls.append(kwargs)

    class FakeFn:
        spawn = FakeSpawn()

        @classmethod
        def from_name(cls, *args, **kwargs):
            return FakeFn()

    fake_modal = mock.Mock()
    fake_modal.Function = FakeFn
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    monkeypatch.setenv("CRON_SECRET", "test-secret")
    monkeypatch.setattr(
        "core.pulse.briefing.due_tenant_ids", lambda trigger: ["tenant-1"]
    )

    response = client.post(
        "/api/pulse-cron", headers={"Authorization": "Bearer test-secret"}
    )

    assert response.status_code == 200
    assert calls == [
        {"uid": "tenant-1", "auth_secret": "test-secret", "trigger": "cron"}
    ]
