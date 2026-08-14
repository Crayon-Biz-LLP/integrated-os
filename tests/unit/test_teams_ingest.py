"""
Unit tests for core.skills.teams_ingest — direction awareness + sieve.

Covered (pure logic / mocked — no network):
  - is_own_message: the user's own sends detected by Graph user id; None
    identity (me fetch failed) degrades to incoming (today's behavior)
  - fetch_me: /me returns id + displayName; failure returns None (never
    crashes the tick)
  - sieve stage: deterministic noise dropped before the LLM classify call
  - record_outgoing_message is called for own sends (not classify+ingest)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.skills.teams_ingest import (
    fetch_me,
    is_own_message,
    ingest_teams_messages,
)


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
        return None

    def json(self):
        return self.data


# ── is_own_message (pure) ─────────────────────────────────────────────

def test_own_message_matches_me_id():
    me = {"id": "user-abc", "displayName": "Daniel"}
    assert is_own_message("user-abc", me) is True


def test_own_message_other_sender_is_incoming():
    me = {"id": "user-abc", "displayName": "Daniel"}
    assert is_own_message("user-xyz", me) is False


def test_own_message_unknown_me_degrades_to_incoming():
    # me fetch failed → None → treat as incoming (today's behavior)
    assert is_own_message("user-abc", None) is False


def test_own_message_missing_ids():
    assert is_own_message("", {"id": "user-abc"}) is False
    assert is_own_message("user-abc", {"id": None}) is False


# ── fetch_me ──────────────────────────────────────────────────────────

@patch("core.skills.teams_ingest.requests.get")
def test_fetch_me_returns_identity(mock_get):
    mock_get.return_value = _FakeResponse({"id": "user-abc", "displayName": "Daniel"})
    me = asyncio.run(fetch_me("tok"))
    assert me == {"id": "user-abc", "displayName": "Daniel"}


@patch("core.skills.teams_ingest.requests.get")
def test_fetch_me_missing_id_returns_none(mock_get):
    mock_get.return_value = _FakeResponse({"displayName": "Daniel"})
    assert asyncio.run(fetch_me("tok")) is None


@patch("core.skills.teams_ingest.requests.get")
def test_fetch_me_http_failure_returns_none(mock_get):
    mock_get.side_effect = RuntimeError("boom")
    assert asyncio.run(fetch_me("tok")) is None


# ── ingest flow: own send → record_outgoing_message ───────────────────

def _msg(sender_id, body, mid="m1"):
    return {
        "id": mid,
        "from": {"user": {"id": sender_id, "displayName": "Daniel" if sender_id == "me-1" else "Bob"}},
        "body": {"content": body},
        "createdDateTime": "2026-08-14T10:00:00Z",
    }


def test_own_send_routes_to_outgoing_not_classify():
    """The user's own Teams message must NOT go through the LLM classifier
    (which would surface it as incoming FYI) — it records as outgoing."""
    fake_sb = MagicMock()
    fake_sb.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=[])
    fake_chain = MagicMock()
    fake_sb.table.return_value = fake_chain
    # dup check: no data
    fake_chain.select.return_value.eq.return_value.filter.return_value.execute.return_value = MagicMock(data=[])

    chats = [{"id": "chat-1", "chatType": "oneOnOne"}]
    messages = [
        _msg("me-1", "I'll send the deck over", "m-own"),
        _msg("bob-1", "Can you review this by EOD?", "m-bob"),
    ]

    record_outgoing = AsyncMock(return_value={"status": "filed", "action": "outgoing"})
    classify = AsyncMock(return_value={"classification": "fyi", "summary": "x"})

    with patch("core.skills.teams_ingest.refresh_outlook_token",
               return_value={"access_token": "tok"}), \
         patch("core.skills.teams_ingest.fetch_me",
               return_value={"id": "me-1", "displayName": "Daniel"}), \
         patch("core.skills.teams_ingest.fetch_teams_chats",
               return_value=chats), \
         patch("core.skills.teams_ingest.fetch_chat_messages",
               return_value=messages), \
         patch("core.skills.teams_ingest.classify_teams_message", classify), \
         patch("core.lib.ingest.ingest", AsyncMock()), \
         patch("core.lib.ingest.record_outgoing_message", record_outgoing), \
         patch("core.skills.teams_ingest.supabase", fake_sb):
        result = asyncio.run(ingest_teams_messages())

    # Own message → record_outgoing, NOT classify/ingest
    record_outgoing.assert_awaited_once()
    call_kwargs = record_outgoing.await_args.kwargs
    assert call_kwargs["source"] == "teams"
    assert call_kwargs["chat_id"] == "chat-1"
    assert call_kwargs["body"] == "I'll send the deck over"
    assert "teams_message_id" in call_kwargs["metadata"]
    # Classifier only saw Bob's incoming message (the own send never reached it)
    classify.assert_awaited_once()
    assert result["outgoing"] == 1
    assert result["processed"] == 1  # Bob's message processed normally


def test_sieve_drops_noise_before_llm():
    """Reaction/media-only noise is dropped deterministically — the LLM
    classifier must not be called for it (intelligence parity with
    WhatsApp/Beeper)."""
    fake_sb = MagicMock()
    fake_chain = MagicMock()
    fake_sb.table.return_value = fake_chain
    fake_chain.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=[])
    fake_chain.select.return_value.eq.return_value.filter.return_value.execute.return_value = MagicMock(data=[])

    chats = [{"id": "chat-1", "chatType": "group"}]
    messages = [
        _msg("bob-1", "👍", "m-react"),
        _msg("bob-1", "Thanks!", "m-thanks"),
        _msg("bob-1", "Can you share the OOS sheet?", "m-real"),
    ]

    classify = AsyncMock(return_value={"classification": "fyi", "summary": "x"})

    with patch("core.skills.teams_ingest.refresh_outlook_token",
               return_value={"access_token": "tok"}), \
         patch("core.skills.teams_ingest.fetch_me",
               return_value={"id": "me-1", "displayName": "Daniel"}), \
         patch("core.skills.teams_ingest.fetch_teams_chats",
               return_value=chats), \
         patch("core.skills.teams_ingest.fetch_chat_messages",
               return_value=messages), \
         patch("core.skills.teams_ingest.classify_teams_message", classify), \
         patch("core.lib.ingest.ingest", AsyncMock()), \
         patch("core.lib.ingest.record_outgoing_message", AsyncMock()), \
         patch("core.skills.teams_ingest.supabase", fake_sb):
        result = asyncio.run(ingest_teams_messages())

    assert classify.await_count == 1  # only the real message reached the LLM
    assert result["ignored"] == 2  # reaction + thanks dropped by sieve
