"""Hermetic tests for FCM push orchestration (`core/services/push_notification.py`).

Covers:
  - push_data_content: byte-aware truncation under the 4KB FCM cap — never
    splits a multi-byte UTF-8 char (emoji / Indic script), empty passthrough,
    exact-fit passthrough.
  - send_push_notification: no-creds skip, per-token fan-out, token dedup
    (a re-registered device must not double-notify), platform-specific config
    (android high priority / ios apns sound+badge), data payload stringification,
    and 404 invalid-token cleanup (owner-scoped).
  - send_silent_push: data-only payload (no notification field), android
    priority, 404 cleanup.

FCM credentials, token rows and the HTTP client are all mocked — no network.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.services.push_notification import (
    push_data_content,
    send_push_notification,
    send_silent_push,
)

pytestmark = pytest.mark.sync


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient: records posts, returns canned responses."""

    def __init__(self, responses):
        self.post = AsyncMock(side_effect=responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------- push_data_content

def test_push_data_content_empty_and_short_passthrough():
    assert push_data_content("") == ""
    assert push_data_content("hello") == "hello"


def test_push_data_content_exact_fit_unchanged():
    text = "a" * 2800
    assert push_data_content(text) == text


def test_push_data_content_truncates_long_ascii_to_byte_budget():
    text = "x" * 5000
    result = push_data_content(text)
    assert len(result) == 2800
    assert len(result.encode("utf-8")) == 2800


def test_push_data_content_never_splits_multibyte_characters():
    # 4-byte emoji: a cut at byte 2799 lands mid-character — must back off.
    text = "😀" * 2000  # 8000 bytes
    result = push_data_content(text)
    encoded = result.encode("utf-8")
    assert len(encoded) <= 2800
    assert len(encoded) > 2790  # backed off only the partial char, not more
    assert result.endswith("😀")
    # every char decodes whole — no U+FFFD replacement from a split sequence
    assert "\ufffd" not in result


def test_push_data_content_cjk_boundary():
    text = "汉" * 2000  # 3 bytes each → 6000 bytes
    result = push_data_content(text, max_bytes=1000)
    encoded = result.encode("utf-8")
    assert len(encoded) <= 1000
    assert len(encoded) > 995
    assert result.endswith("汉")
    assert "\ufffd" not in result


# ------------------------------------------------- send_push_notification

def _fake_creds():
    creds = MagicMock()
    creds.token = "access-tok"
    creds.project_id = "rhodey-os"
    return creds


def test_push_no_creds_skips_silently():
    with patch("core.services.push_notification._get_fcm_credentials", return_value=None), \
         patch("core.services.push_notification.audit_log_sync") as audit:
        n = _run(send_push_notification("Title", "Body"))
    assert n == 0
    # logged as a skip, not an error
    assert any("not set" in c.args[2] for c in audit.call_args_list)


def test_push_fanout_dedups_tokens_and_configures_platforms():
    creds = _fake_creds()
    tokens_res = MagicMock()
    tokens_res.data = [
        {"token": "android-tok", "platform": "android"},
        {"token": "android-tok", "platform": "android"},  # re-registered dup
        {"token": "ios-tok", "platform": "ios"},
    ]
    query = MagicMock()
    query.execute.return_value = tokens_res
    client = _FakeAsyncClient([_Resp(200), _Resp(200)])

    with patch("core.services.push_notification._get_fcm_credentials", return_value=creds), \
         patch("core.services.push_notification.scoped_tokens_query", return_value=query), \
         patch("core.services.push_notification.httpx.AsyncClient", return_value=client), \
         patch("core.services.push_notification.audit_log_sync"):
        n = _run(send_push_notification("New briefing", "Your morning brief is ready",
                                        data={"type": "briefing", "count": 3}))

    assert n == 2  # deduped: 2 devices, not 3
    assert client.post.await_count == 2
    messages = [call.kwargs["json"]["message"] for call in client.post.await_args_list]
    by_token = {m["token"]: m for m in messages}
    assert set(by_token) == {"android-tok", "ios-tok"}

    android = by_token["android-tok"]
    assert android["notification"] == {"title": "New briefing", "body": "Your morning brief is ready"}
    assert android["android"] == {"priority": "high"}
    assert "apns" not in android
    # data payload values stringified
    assert android["data"] == {"type": "briefing", "count": "3"}

    ios = by_token["ios-tok"]
    assert ios["apns"]["payload"]["aps"] == {"sound": "default", "badge": 1}
    assert "android" not in ios


def test_push_404_cleans_up_invalid_token_owner_scoped():
    creds = _fake_creds()
    tokens_res = MagicMock()
    tokens_res.data = [{"token": "dead-tok", "platform": "android"}]
    query = MagicMock()
    query.execute.return_value = tokens_res
    client = _FakeAsyncClient([_Resp(404)])
    supabase = MagicMock()

    with patch("core.services.push_notification._get_fcm_credentials", return_value=creds), \
         patch("core.services.push_notification.scoped_tokens_query", return_value=query), \
         patch("core.services.push_notification.httpx.AsyncClient", return_value=client), \
         patch("core.services.push_notification.get_supabase", return_value=supabase), \
         patch("core.services.push_notification.get_tenant", return_value="tenant-uid"), \
         patch("core.services.push_notification.audit_log_sync"):
        n = _run(send_push_notification("t", "b"))

    assert n == 0
    delete_q = supabase.table.return_value.delete.return_value
    delete_q.in_.assert_called_once_with("token", ["dead-tok"])
    # owner-scoped cleanup (M4) — never delete another tenant's tokens
    delete_q.in_.return_value.eq.assert_called_once_with("owner_id", "tenant-uid")
    delete_q.in_.return_value.eq.return_value.execute.assert_called_once()


# ------------------------------------------------- send_silent_push

def test_silent_push_is_data_only_with_priority():
    creds = _fake_creds()
    tokens_res = MagicMock()
    tokens_res.data = [{"token": "t1", "platform": "android"}]
    query = MagicMock()
    query.execute.return_value = tokens_res
    client = _FakeAsyncClient([_Resp(200)])

    with patch("core.services.push_notification._get_fcm_credentials", return_value=creds), \
         patch("core.services.push_notification.scoped_tokens_query", return_value=query), \
         patch("core.services.push_notification.httpx.AsyncClient", return_value=client), \
         patch("core.services.push_notification.get_tenant", return_value="tenant-uid"):
        n = _run(send_silent_push({"type": "briefing_refresh"}))

    assert n == 1
    message = client.post.await_args.kwargs["json"]["message"]
    # no visible notification — silent by contract
    assert "notification" not in message
    assert message["data"] == {"type": "briefing_refresh"}
    assert message["android"] == {"priority": "high"}


def test_silent_push_404_cleanup():
    creds = _fake_creds()
    tokens_res = MagicMock()
    tokens_res.data = [{"token": "dead-tok", "platform": "android"}]
    query = MagicMock()
    query.execute.return_value = tokens_res
    client = _FakeAsyncClient([_Resp(404)])
    supabase = MagicMock()

    with patch("core.services.push_notification._get_fcm_credentials", return_value=creds), \
         patch("core.services.push_notification.scoped_tokens_query", return_value=query), \
         patch("core.services.push_notification.httpx.AsyncClient", return_value=client), \
         patch("core.services.push_notification.get_supabase", return_value=supabase), \
         patch("core.services.push_notification.get_tenant", return_value="tenant-uid"):
        n = _run(send_silent_push({"type": "briefing_refresh"}))

    assert n == 0
    supabase.table.return_value.delete.return_value.in_.assert_called_once_with("token", ["dead-tok"])
