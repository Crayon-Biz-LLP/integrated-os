"""Security negatives — OTP + API-key auth (auth aspect).

The positive path (provisioning, key issuance) is covered by
test_auth_provision; this suite pins the NEGATIVES that protect the
boundary:

  - OTP: 5-attempt cap (burn-one-try increments BEFORE validation), the
    60s resend rate limit, expiry, consumed-code rejection, the
    ANTI-ENUMERATION parity rule (unknown email, no code, wrong code, and
    reused code all return the identical message), and the daily send cap.
  - API key: unknown key resolves to None, the fail-closed 503 when
    API_SECRET_KEY is unset (no ALLOW_DEV_AUTH), 401 on a wrong key,
    per-user key scoping, and a DISABLED user's key not granting access.

All DB/email calls are mocked — no network, no real rows.
"""

import datetime
import pytest
from unittest.mock import MagicMock, patch

from core.services import auth
from core.services.db import resolve_user_by_api_key
from api.index import require_api_auth

pytestmark = pytest.mark.auth


# ── fixtures ───────────────────────────────────────────────────────────────

def _otp_row(**overrides):
    row = {
        "id": 1,
        "email": "new@acme.com",
        "code_hash": auth._hash_code("new@acme.com", "123456"),
        "attempts": 0,
        "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=9)).isoformat(),
        "consumed_at": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    row.update(overrides)
    return row


def _mock_supabase():
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
    return sb


# ── OTP: send negatives ────────────────────────────────────────────────────

def test_send_otp_rejects_invalid_email():
    res = auth.send_otp("not-an-email")
    assert res["ok"] is False
    assert "valid email" in res["message"]


def test_send_otp_rate_limited_within_60s_gap():
    recent = _otp_row(created_at=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)).isoformat())
    with patch.object(auth, "_latest_otp", return_value=recent), \
         patch("core.services.otp_email.send_otp_email", return_value=True) as send:
        res = auth.send_otp("new@acme.com")
    assert res["ok"] is False
    assert "wait a minute" in res["message"]
    send.assert_not_called()


def test_send_otp_allows_resend_after_gap():
    old = _otp_row(created_at=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)).isoformat())
    sb = _mock_supabase()
    with patch.object(auth, "_latest_otp", return_value=old), \
         patch("core.services.otp_email.send_otp_email", return_value=True) as send, \
         patch.object(auth, "get_supabase", return_value=sb), \
         patch.object(auth, "_find_user_by_email", return_value=None):
        res = auth.send_otp("new@acme.com")
    assert res["ok"] is True
    send.assert_called_once()
    # the new row was inserted
    sb.table.return_value.insert.assert_called_once()


def test_send_otp_daily_cap_blocks():
    sb = _mock_supabase()
    # daily count query returns >= cap rows
    sb.table.return_value.select.return_value.gte.return_value.execute.return_value = MagicMock(data=[{"id": 1}] * 100)
    with patch.object(auth, "_latest_otp", return_value=None), \
         patch.object(auth, "_find_user_by_email", return_value=None), \
         patch.object(auth, "get_supabase", return_value=sb), \
         patch.dict("os.environ", {"OTP_DAILY_SEND_CAP": "100"}, clear=False), \
         patch("core.services.otp_email.send_otp_email", return_value=True) as send:
        res = auth.send_otp("new@acme.com")
    assert res["ok"] is False
    assert "Too many sign-in requests" in res["message"]
    send.assert_not_called()


def test_send_otp_disabled_user_never_emailed():
    sb = _mock_supabase()
    with patch.object(auth, "_latest_otp", return_value=None), \
         patch.object(auth, "get_supabase", return_value=sb), \
         patch.object(auth, "_find_user_by_email", return_value={"id": "u1", "email": "old@acme.com", "status": "disabled"}), \
         patch("core.services.otp_email.send_otp_email", return_value=True) as send:
        res = auth.send_otp("old@acme.com")
    assert res["ok"] is True  # indistinguishable from a real send (no enumeration)
    # a code row IS created (insert happens before the user check), but the
    # email is never sent — the guard is on DELIVERY, not code creation
    sb.table.return_value.insert.assert_called_once()
    send.assert_not_called()


# ── OTP: verify negatives ──────────────────────────────────────────────────

def test_verify_wrong_code_increments_attempts_and_rejects():
    otp = _otp_row(attempts=0)
    sb = _mock_supabase()
    with patch.object(auth, "_latest_otp", return_value=otp), \
         patch.object(auth, "_find_user_by_email", return_value=None), \
         patch.object(auth, "get_supabase", return_value=sb):
        res = auth.verify_otp("new@acme.com", "000000")
    assert res["ok"] is False
    assert res["message"] == "Invalid code or email."
    # burn-one-try: attempts incremented BEFORE validation
    sb.table.return_value.update.assert_called_once_with({"attempts": 1})


def test_verify_at_max_attempts_rejected_without_burn():
    otp = _otp_row(attempts=5)
    sb = _mock_supabase()
    with patch.object(auth, "_latest_otp", return_value=otp), \
         patch.object(auth, "_find_user_by_email", return_value=None), \
         patch.object(auth, "get_supabase", return_value=sb):
        res = auth.verify_otp("new@acme.com", "123456")  # even the RIGHT code
    assert res["ok"] is False
    assert "Too many wrong attempts" in res["message"]
    sb.table.return_value.update.assert_not_called()  # no burn at the cap


def test_verify_expired_code_rejected():
    otp = _otp_row(expires_at=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat())
    with patch.object(auth, "_latest_otp", return_value=otp), \
         patch.object(auth, "_find_user_by_email", return_value=None):
        res = auth.verify_otp("new@acme.com", "123456")
    assert res["ok"] is False
    assert "expired" in res["message"]


def test_verify_consumed_code_anti_enumeration():
    otp = _otp_row(consumed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    with patch.object(auth, "_latest_otp", return_value=otp), \
         patch.object(auth, "_find_user_by_email", return_value=None):
        res = auth.verify_otp("new@acme.com", "123456")
    assert res["message"] == "Invalid code or email."


def test_verify_unknown_email_parity_with_wrong_code():
    """Anti-enumeration: an email with NO code row must return the SAME
    message as a wrong code — callers cannot probe which emails exist."""
    with patch.object(auth, "_latest_otp", return_value=None), \
         patch.object(auth, "_find_user_by_email", return_value=None):
        no_code = auth.verify_otp("nobody@acme.com", "123456")
    with patch.object(auth, "_latest_otp", return_value=_otp_row(attempts=0)), \
         patch.object(auth, "_find_user_by_email", return_value=None), \
         patch.object(auth, "get_supabase", return_value=_mock_supabase()):
        wrong = auth.verify_otp("new@acme.com", "000000")
    assert no_code["message"] == wrong["message"] == "Invalid code or email."


def test_verify_requires_email_and_code():
    assert auth.verify_otp("", "")["ok"] is False
    assert "required" in auth.verify_otp("", "")["message"]


# ── API key negatives ──────────────────────────────────────────────────────

def test_resolve_unknown_api_key_returns_none():
    sb = MagicMock()  # no-match row (single .eq chain used by resolve)
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
    with patch("core.services.db.get_supabase", return_value=sb):
        assert resolve_user_by_api_key("key-that-does-not-exist") is None


def test_resolve_api_key_table_missing_returns_none():
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.side_effect = Exception("relation \"users\" does not exist")
    with patch("core.services.db.get_supabase", return_value=sb), \
         patch("core.lib.audit_logger.audit_log_sync"):
        assert resolve_user_by_api_key("any-key") is None  # fail-closed, no crash


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_require_auth_fails_closed_when_shared_key_unset():
    """No API_SECRET_KEY + no ALLOW_DEV_AUTH → 503. A missing env var in
    prod must never leave the API open."""
    from fastapi import HTTPException
    with patch.dict("os.environ", {}, clear=True), \
         patch("api.index.resolve_user_by_api_key", return_value=None):
        with pytest.raises(HTTPException) as exc:
            require_api_auth(_Req({"X-API-Key": "anything"}))
    assert exc.value.status_code == 503


def test_require_auth_rejects_wrong_key():
    from fastapi import HTTPException
    with patch.dict("os.environ", {"API_SECRET_KEY": "shared-secret", "ALLOW_DEV_AUTH": "0"}, clear=False), \
         patch("api.index.resolve_user_by_api_key", return_value=None):
        with pytest.raises(HTTPException) as exc:
            require_api_auth(_Req({"X-API-Key": "wrong-key"}))
    assert exc.value.status_code == 401


def test_require_auth_per_user_key_scopes_tenant():
    with patch.dict("os.environ", {"API_SECRET_KEY": "shared-secret"}, clear=False), \
         patch("api.index.resolve_user_by_api_key", return_value={"id": "uid-9", "status": "active"}) as resolve, \
         patch("api.index.set_tenant") as set_tn:
        uid = require_api_auth(_Req({"X-API-Key": "user-key"}))
    resolve.assert_called_once_with("user-key")
    set_tn.assert_called_once_with("uid-9")
    assert uid == "uid-9"


def test_require_auth_disabled_user_key_does_not_scope():
    """A DISABLED user's key must not set tenant context — the request falls
    through to the shared-key path (401 here since the key isn't shared)."""
    from fastapi import HTTPException
    with patch.dict("os.environ", {"API_SECRET_KEY": "shared-secret"}, clear=False), \
         patch("api.index.resolve_user_by_api_key", return_value={"id": "uid-x", "status": "disabled"}), \
         patch("api.index.set_tenant") as set_tn:
        with pytest.raises(HTTPException) as exc:
            require_api_auth(_Req({"X-API-Key": "disabled-user-key"}))
    assert exc.value.status_code == 401
    set_tn.assert_not_called()
