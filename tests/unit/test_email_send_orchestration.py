"""Hermetic tests for email SEND orchestration (`core/webhook/email.py`).

Covers:
  - _send_draft_reply (Gmail): threading headers (In-Reply-To/References
    from the original Message-ID, threadId fallback), reply-all CC
    collection (excluding sender + self), status→'sent' set BEFORE the API
    call (the double-send guard), send-failure keeps 'sent', learning
    observation on success, not-found paths.
  - send_outlook_draft: 202 success, 401 refresh-and-retry, no-token fail.

The supabase client, Gmail service and HTTP client are mocked — no network,
no DB. Draft rows are fed through `maybe_single_safe` directly.
"""

import asyncio
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.webhook.email import _send_draft_reply, send_outlook_draft

pytestmark = pytest.mark.email


def _run(coro):
    return asyncio.run(coro)


def _draft_row(**overrides):
    row = {
        "id": 5,
        "message_id": 101,
        "draft_body": "Sure, sending the deck shortly.",
        "status": "pending",
        "messages": {
            "sender_id": "boss@acme.com",
            "thread_id": "thread-abc",
            "source": "gmail",
            "subject": "Q3 deck",
            "message_id": "orig-msg-1",
        },
    }
    row.update(overrides)
    return row


def _mock_maybe_single_safe(row):
    res = MagicMock()
    res.data = row
    return res


def _gmail_service(original_headers=None, send_result=None, send_error=None):
    """Gmail service mock. original_headers = the metadata payload headers."""
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    if original_headers is not None:
        messages.get.return_value.execute.return_value = {
            "payload": {"headers": original_headers}
        }
    else:
        messages.get.return_value.execute.side_effect = Exception("no original")
    if send_error is not None:
        messages.send.return_value.execute.side_effect = send_error
    elif send_result is not None:
        messages.send.return_value.execute.return_value = send_result
    else:
        messages.send.return_value.execute.return_value = {"id": "sent-1"}
    return service


def _decode_sent_raw(service):
    """Decode the raw MIME message captured on the gmail send call."""
    send_call = service.users.return_value.messages.return_value.send.call_args
    raw = send_call.kwargs["body"]["raw"]
    return base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8")


# ------------------------------------------------------------ Gmail send

def test_gmail_send_success_threads_and_returns_true(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "danny@rhodey.ai")
    service = _gmail_service(original_headers=[{"name": "Message-ID", "value": "<orig123@acme.com>"}])
    supabase = MagicMock()

    with patch("core.webhook.email.maybe_single_safe", return_value=_mock_maybe_single_safe(_draft_row())), \
         patch("core.webhook.email.get_gmail_service", return_value=service), \
         patch("core.webhook.email.supabase", supabase), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()) as emit:
        ok, err = _run(_send_draft_reply(5))

    assert ok is True
    assert err is None
    # threading headers from the original Message-ID
    decoded = _decode_sent_raw(service)
    assert "To: boss@acme.com" in decoded
    assert "Subject: Re: Q3 deck" in decoded
    assert "In-Reply-To: <orig123@acme.com>" in decoded
    assert "References: <orig123@acme.com>" in decoded
    # threadId rides on the send envelope
    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs["body"]
    assert send_body["threadId"] == "thread-abc"
    # status flipped to 'sent' (double-send guard) + learning signal emitted
    supabase.table.return_value.update.assert_called_once_with({"status": "sent"})
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["outcome"] == "confirmed"


def test_gmail_send_reply_all_collects_cc_excluding_sender_and_self(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "danny@rhodey.ai")
    service = _gmail_service(original_headers=[
        {"name": "Message-ID", "value": "<orig123@acme.com>"},
        {"name": "To", "value": "boss@acme.com, team@acme.com"},
        {"name": "Cc", "value": "manager@acme.com, danny@rhodey.ai"},
    ])

    with patch("core.webhook.email.maybe_single_safe", return_value=_mock_maybe_single_safe(_draft_row())), \
         patch("core.webhook.email.get_gmail_service", return_value=service), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()):
        ok, _ = _run(_send_draft_reply(5))

    assert ok is True
    decoded = _decode_sent_raw(service)
    # boss excluded (the sender), danny excluded (self) — team + manager stay
    assert "Cc: team@acme.com, manager@acme.com" in decoded
    assert "boss@acme.com" not in decoded.split("Cc:")[1].split("\r\n")[0]


def test_gmail_send_fallback_threading_when_original_unfetchable(monkeypatch):
    """Original-message fetch fails → threadId fallback for threading headers."""
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "danny@rhodey.ai")
    service = _gmail_service(original_headers=None)  # get() raises

    with patch("core.webhook.email.maybe_single_safe", return_value=_mock_maybe_single_safe(_draft_row())), \
         patch("core.webhook.email.get_gmail_service", return_value=service), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync") as audit, \
         patch("core.webhook.email.emit_observation", new=AsyncMock()):
        ok, _ = _run(_send_draft_reply(5))

    assert ok is True
    decoded = _decode_sent_raw(service)
    assert "In-Reply-To: thread-abc" in decoded
    assert "References: thread-abc" in decoded
    # the failed header fetch was logged, not fatal
    assert any(call.args[1] == "WARNING" for call in audit.call_args_list)


def test_gmail_send_failure_marks_status_failed(monkeypatch):
    """Double-send guard: once flipped to 'sent', a failed API call marks the draft
    as 'failed' rather than lying to the user that it was sent."""
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "danny@rhodey.ai")
    service = _gmail_service(original_headers=[{"name": "Message-ID", "value": "<orig123@acme.com>"}],
                             send_error=Exception("gmail 500"))
    supabase = MagicMock()

    with patch("core.webhook.email.maybe_single_safe", return_value=_mock_maybe_single_safe(_draft_row())), \
         patch("core.webhook.email.get_gmail_service", return_value=service), \
         patch("core.webhook.email.supabase", supabase), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()) as emit:
        ok, err = _run(_send_draft_reply(5))

    assert ok is False
    assert "gmail 500" in str(err)
    
    # status was set to 'sent' then 'failed'
    update_calls = supabase.table.return_value.update.call_args_list
    assert len(update_calls) == 2
    assert update_calls[0][0][0] == {"status": "sent"}
    assert update_calls[1][0][0] == {"status": "failed"}
    
    # no 'confirmed' observation for a message that never shipped
    emit.assert_not_awaited()


def test_gmail_send_draft_not_found():
    with patch("core.webhook.email.maybe_single_safe", return_value=None), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync"):
        ok, err = _run(_send_draft_reply(999))
    assert ok is False
    assert "Draft not found" in err


def test_gmail_send_strips_legacy_subject_header_from_body(monkeypatch):
    """Old drafts carried a 'Subject:' first line — strip it before sending."""
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "danny@rhodey.ai")
    row = _draft_row(draft_body="Subject: old header\nSure, sending the deck shortly.")
    service = _gmail_service(original_headers=[{"name": "Message-ID", "value": "<orig123@acme.com>"}])

    with patch("core.webhook.email.maybe_single_safe", return_value=_mock_maybe_single_safe(row)), \
         patch("core.webhook.email.get_gmail_service", return_value=service), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()):
        ok, _ = _run(_send_draft_reply(5))

    assert ok is True
    decoded = _decode_sent_raw(service)
    # MIMEText base64-encodes the body — decode it to check the content
    msg = __import__("email").message_from_string(decoded)
    body = msg.get_payload(decode=True).decode("utf-8")
    assert body == "Sure, sending the deck shortly."
    assert "Subject: old header" not in decoded


# ------------------------------------------------------- Outlook send

class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    def __init__(self, responses):
        self.post = AsyncMock(side_effect=responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _outlook_draft():
    return {
        "id": 7,
        "draft_body": "Outlook reply body",
        "emails": {
            "sender_id": "vendor@external.com",
            "message_id": "graph-msg-1",
            "source": "outlook",
            "subject": "Vendor contract",
        },
    }


def test_outlook_send_success_on_202():
    client = _FakeAsyncClient([_Resp(202)])
    supabase = MagicMock()

    with patch("core.skills.outlook_token_helper.get_outlook_access_token", return_value="tok"), \
         patch("core.webhook.email.httpx.AsyncClient", return_value=client), \
         patch("core.webhook.email.supabase", supabase), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()) as emit:
        ok, err = _run(send_outlook_draft(_outlook_draft()))

    assert ok is True
    assert err is None
    # status→sent before the API call (double-send guard)
    supabase.table.return_value.update.assert_called_once_with({"status": "sent"})
    # replyAll against the original message, text body
    post_kwargs = client.post.call_args
    assert post_kwargs.args[0] == "https://graph.microsoft.com/v1.0/me/messages/graph-msg-1/replyAll"
    assert post_kwargs.kwargs["json"] == {
        "message": {"body": {"contentType": "Text", "content": "Outlook reply body"}}
    }
    assert post_kwargs.kwargs["headers"]["Authorization"] == "Bearer tok"
    emit.assert_awaited_once()


def test_outlook_send_401_refreshes_and_retries():
    client = _FakeAsyncClient([_Resp(401), _Resp(202)])

    with patch("core.skills.outlook_token_helper.get_outlook_access_token", return_value="stale-tok"), \
         patch("core.skills.outlook_token_helper.refresh_outlook_token",
               return_value={"access_token": "fresh-tok"}) as refresh, \
         patch("core.webhook.email.httpx.AsyncClient", return_value=client), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync"), \
         patch("core.webhook.email.emit_observation", new=AsyncMock()):
        ok, err = _run(send_outlook_draft(_outlook_draft()))

    assert ok is True
    assert err is None
    refresh.assert_called_once_with(write_back=True)
    assert client.post.await_count == 2
    # retry carried the fresh token
    retry_auth = client.post.await_args_list[1].kwargs["headers"]["Authorization"]
    assert retry_auth == "Bearer fresh-tok"


def test_outlook_send_no_token_fails_cleanly():
    with patch("core.skills.outlook_token_helper.get_outlook_access_token", return_value=None), \
         patch("core.webhook.email.audit_log_sync"):
        ok, err = _run(send_outlook_draft(_outlook_draft()))
    assert ok is False
    assert "Outlook is not connected" in err


def test_outlook_send_202_refresh_failure_reports():
    client = _FakeAsyncClient([_Resp(401), _Resp(401)])

    with patch("core.skills.outlook_token_helper.get_outlook_access_token", return_value="stale-tok"), \
         patch("core.skills.outlook_token_helper.refresh_outlook_token", return_value=None), \
         patch("core.webhook.email.httpx.AsyncClient", return_value=client), \
         patch("core.webhook.email.supabase", MagicMock()), \
         patch("core.webhook.email.audit_log_sync"):
        ok, err = _run(send_outlook_draft(_outlook_draft()))

    assert ok is False
    assert "could not be refreshed" in err
