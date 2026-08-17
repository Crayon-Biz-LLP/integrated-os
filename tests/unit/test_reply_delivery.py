"""Unit tests for the app-reply delivery boundary (core/services/reply_delivery.py).

Pins the two behaviors from the Aug-17 tenant audit fix:
  - persist_app=False → admin-only alert: the app channel (raw_dumps persist
    + FCM push) is skipped entirely (health-check alert product decision:
    system alerts are for the admin on Telegram, never the app).
  - Unscoped persists resolve the channel tenant via channel_tenant_scope()
    instead of failing closed — the boundary fix for cron/script senders
    (health check, research agent, roundup) that were silently dropping
    their replies from the app history (10x "NO TENANT SCOPE" audit errors
    in 5 days).
"""
import contextlib
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.sync


@pytest.fixture(autouse=True)
def _clean_tenant_state():
    """Ensure the tenant context + channel-tenant cache are clean between tests."""
    yield
    from core.services import db as db_mod
    db_mod._tenant_var.set(None)
    db_mod._channel_tenant = None


@pytest.mark.asyncio
async def test_persist_app_false_skips_app_channel_entirely():
    """Admin-only alert: no raw_dumps persist, no push, returns 0 devices."""
    from core.services import reply_delivery

    with (
        patch.object(reply_delivery, "_persist_to_raw_dumps") as persist,
        patch("core.services.push_notification.send_push_notification") as push,
    ):
        result = await reply_delivery.deliver_outbound_reply(
            "⚠️ Health check alert", notify_push=True, persist_app=False
        )

    persist.assert_not_called()
    push.assert_not_called()
    assert result == 0


@pytest.mark.asyncio
async def test_persist_app_true_still_persists():
    """Default behavior unchanged: normal messages still persist to raw_dumps."""
    from core.services import reply_delivery

    with (
        patch.object(reply_delivery, "_persist_to_raw_dumps") as persist,
        patch("core.services.push_notification.send_push_notification"),
    ):
        result = await reply_delivery.deliver_outbound_reply(
            "Task created", notify_push=False, persist_app=True
        )

    persist.assert_called_once()
    assert result == 0  # notify_push=False → no push, but persist happened


@pytest.mark.asyncio
async def test_unscoped_persist_resolves_channel_tenant():
    """Cron sender with NO tenant context: the write lands under the resolved
    channel tenant instead of failing closed with TenantRequiredError."""
    from core.services import db as db_mod
    from core.services import reply_delivery

    @contextlib.contextmanager
    def fake_channel_scope():
        # Mirrors the real channel_tenant_scope(): no-arg, applies the scope.
        with db_mod.tenant_scope("chan-uid"):
            yield

    mock_client = MagicMock()

    def _execute_check():
        # The facade write must run INSIDE the resolved tenant scope.
        assert db_mod.get_tenant() == "chan-uid", "write ran outside tenant scope"
        return MagicMock(data=[{"id": 1}])

    mock_client.table.return_value.insert.return_value.execute.side_effect = _execute_check

    # Note: get_tenant/tenant_mode_enabled are NOT patched — the happy path
    # never reads them, and patching get_tenant would break the real
    # tenant_scope() contextvar machinery the fake scope relies on.
    with (
        patch.object(db_mod, "channel_tenant_scope", fake_channel_scope),
        patch.object(db_mod, "tenant_aware_client", return_value=mock_client),
        patch("core.services.reply_delivery.audit_log_sync") as audit,
    ):
        await reply_delivery.deliver_outbound_reply("Cron notice", notify_push=False)

    mock_client.table.assert_any_call("raw_dumps")
    mock_client.table.return_value.insert.assert_called_once()
    audit.assert_not_called()  # no failure path hit
