"""M13 self-serve sign-up: verify_otp auto-provisions unknown emails.

These tests monkeypatch every DB touch — no network, no live Supabase.
They lock in the open-registration contract:
  - a verified, consumed code for an UNKNOWN email creates the tenant
  - provisioning only happens after code validation (never before)
  - the MAX_TENANTS env cap blocks new provisions at the limit
  - provision_user is idempotent for existing emails
"""

from datetime import datetime, timedelta, timezone

import core.services.auth as auth


class _FakeTable:
    """Minimal chainable supabase table stub (select/update/insert paths)."""

    def __init__(self, name: str, count_rows: int = 0):
        self.name = name
        self._count_rows = count_rows
        self._mode = None

    def select(self, *args):
        self._mode = "select"
        return self

    def update(self, data):
        self._mode = "write"
        return self

    def insert(self, data):
        self._mode = "write"
        return self

    def eq(self, *args):
        return self

    def gte(self, *args):
        return self

    def execute(self):
        if self._mode == "select":
            return type("R", (), {"data": [{"id": "x"}] * self._count_rows})()
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, count_rows: int = 0):
        self._count_rows = count_rows

    def table(self, name: str):
        return _FakeTable(name, count_rows=self._count_rows)


def _valid_otp_row(email: str, code: str) -> dict:
    return {
        "id": 1,
        "code_hash": auth._hash_code(email, code),
        "attempts": 0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "consumed_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def test_verify_otp_provisions_unknown_email(monkeypatch):
    """A verified code for an unknown email creates the account + key."""
    email = "newuser@example.com"
    code = "123456"

    monkeypatch.setattr(auth, "_find_user_by_email", lambda e: None)
    monkeypatch.setattr(auth, "_latest_otp", lambda e: _valid_otp_row(email, code))
    created = {"id": "uid-1", "name": "newuser", "email": email, "status": "active"}
    monkeypatch.setattr(auth, "provision_user", lambda e, name_hint=None: created)
    monkeypatch.setattr(auth, "issue_api_key", lambda uid: "key-abc")
    monkeypatch.setattr(auth, "get_supabase", lambda: _FakeClient())

    res = auth.verify_otp(email, code)

    assert res["ok"] is True
    assert res["api_key"] == "key-abc"
    assert res["name"] == "newuser"


def test_verify_otp_wrong_code_never_provisions(monkeypatch):
    """A WRONG code must not provision — provisioning is gated on validation."""
    email = "newuser@example.com"
    right_code = "123456"

    provisioned = []

    monkeypatch.setattr(auth, "_find_user_by_email", lambda e: None)
    monkeypatch.setattr(auth, "_latest_otp", lambda e: _valid_otp_row(email, right_code))
    monkeypatch.setattr(
        auth, "provision_user",
        lambda e, name_hint=None: provisioned.append(e) or {"id": "uid-1", "name": "x", "email": e},
    )
    monkeypatch.setattr(auth, "get_supabase", lambda: _FakeClient())

    res = auth.verify_otp(email, "000000")  # wrong code

    assert res["ok"] is False
    assert provisioned == []  # provisioning never ran


def test_provision_user_honors_max_tenants_cap(monkeypatch):
    """At the MAX_TENANTS cap, provisioning a NEW user raises TenantLimitReached."""
    monkeypatch.setenv("MAX_TENANTS", "1")
    monkeypatch.setattr(auth, "_find_user_by_email", lambda e: None)
    monkeypatch.setattr(auth, "get_supabase", lambda: _FakeClient(count_rows=1))

    try:
        auth.provision_user("someone@example.com")
        assert False, "expected TenantLimitReached"
    except auth.TenantLimitReached:
        pass


def test_provision_user_malformed_cap_fails_open(monkeypatch):
    """A garbage MAX_TENANTS value must not break sign-up (fails open)."""
    monkeypatch.setenv("MAX_TENANTS", "not-a-number")
    monkeypatch.setattr(auth, "_find_user_by_email", lambda e: None)
    monkeypatch.setattr(auth, "get_supabase", lambda: _FakeClient())

    user = auth.provision_user("someone@example.com")
    # Fails open → attempts the insert → _find_user_by_email re-read returns None
    assert user is None


def test_provision_user_returns_existing(monkeypatch):
    """Provisioning an already-registered email returns the row (idempotent)."""
    existing = {"id": "uid-9", "name": "danny", "email": "daniel@crayonbiz.com", "status": "active"}
    monkeypatch.setattr(auth, "_find_user_by_email", lambda e: existing)

    assert auth.provision_user("daniel@crayonbiz.com") is existing
