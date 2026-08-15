"""
Unit tests for core.services.inbox_feed — the Quick-Confirmations feed
builders that surface actionable channel messages, pending email drafts,
and FYI items to the app Inbox.

These builders close the gap where actionable emails/Teams/WhatsApp/calls
(stored in `messages` with danny_decision IS NULL) never reached the Inbox
because the old feed only read raw_dumps with status='pending'.
"""

import pytest


from unittest.mock import MagicMock

from core.services.inbox_feed import (
    shape_channel_message,
    fetch_pending_channel_messages,
    fetch_pending_drafts,
    fetch_fyi_messages,
)
pytestmark = pytest.mark.decision



class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeChain:
    """Minimal supabase query-builder chain that returns canned data."""

    def __init__(self, data, calls=None):
        self._data = data
        self._calls = calls if calls is not None else []

    def _record(self, name, *args, **kwargs):
        self._calls.append((name, args))
        return self

    def select(self, *a, **k):
        return self._record("select", *a, **k)

    def is_(self, *a, **k):
        return self._record("is_", *a, **k)

    def in_(self, *a, **k):
        return self._record("in_", *a, **k)

    def eq(self, *a, **k):
        return self._record("eq", *a, **k)

    def order(self, *a, **k):
        return self._record("order", *a, **k)

    def limit(self, *a, **k):
        return self._record("limit", *a, **k)

    def execute(self):
        return _FakeResponse(self._data)


def _supabase_with(data):
    """A fake supabase client whose .table('x') returns _FakeChain."""
    fake = MagicMock()
    fake.table.return_value = _FakeChain(data)
    return fake


# ── shape_channel_message ─────────────────────────────────────────

def test_shape_channel_message_email():
    row = {
        "id": 42,
        "channel": "email",
        "suggested_title": "Review the PO",
        "subject": "PO from Srikanth",
        "sender_name": "Srikanth AR",
        "created_at": "2026-08-11T11:22:07+00:00",
        "metadata": {"subject": "PO"},
    }
    shaped = shape_channel_message(row)
    assert shaped["id"] == 42
    assert shaped["source"] == "email"
    assert shaped["status"] == "pending"
    assert shaped["message_type"] == "email_action"
    assert shaped["content"] == "Review the PO"
    assert shaped["sender"] == "Srikanth AR"


def test_shape_channel_message_falls_back_to_subject():
    row = {"id": 7, "channel": "teams", "subject": "Confirm deadline"}
    shaped = shape_channel_message(row)
    assert shaped["source"] == "teams"
    assert shaped["message_type"] == "teams_action"
    assert shaped["content"] == "Confirm deadline"


def test_shape_channel_message_untitled_fallback():
    row = {"id": 1, "channel": "call"}
    shaped = shape_channel_message(row)
    assert shaped["content"] == "Untitled"
    assert shaped["status"] == "pending"


def test_shape_channel_message_passes_matrix_event_id_for_beeper():
    # Beeper-sourced rows carry the native Matrix event id in message_id —
    # the app uses its presence to label the card BEEPER (vs legacy WHATSAPP)
    # and to distinguish the source for the reply flow.
    row = {
        "id": 42,
        "channel": "whatsapp",
        "suggested_title": "CNF call?",
        "message_id": "$matrix_event_abc123",
        "metadata": {"chat_id": "919176322898"},
    }
    shaped = shape_channel_message(row)
    assert shaped["message_id"] == "$matrix_event_abc123"


def test_shape_channel_message_omits_message_id_when_absent():
    # MacroDroid-era / email rows have no native event id — the field must be
    # absent so the app does not label them Beeper.
    row = {"id": 7, "channel": "email", "suggested_title": "Review PO"}
    shaped = shape_channel_message(row)
    assert "message_id" not in shaped


def test_shape_channel_message_does_not_leak_email_tracking_id():
    # messages.message_id also holds Gmail tracking ids on email rows — those
    # must NOT be passed through, or the app would label email cards Beeper.
    row = {
        "id": 8,
        "channel": "email",
        "suggested_title": "Review PO",
        "message_id": "msg-gmail-tracking-abc",
    }
    shaped = shape_channel_message(row)
    assert "message_id" not in shaped


# ── fetch_pending_channel_messages ────────────────────────────────

def test_fetch_pending_channel_messages_returns_shaped_rows():
    supabase = _supabase_with([
        {"id": 1, "channel": "email", "suggested_title": "Review PO"},
        {"id": 2, "channel": "teams", "subject": "Confirm deadline"},
    ])
    rows = fetch_pending_channel_messages(supabase)
    assert len(rows) == 2
    assert rows[0]["source"] == "email"
    assert rows[0]["status"] == "pending"
    assert rows[1]["source"] == "teams"


def test_fetch_pending_channel_messages_empty():
    supabase = _supabase_with([])
    assert fetch_pending_channel_messages(supabase) == []


def test_fetch_pending_channel_messages_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    assert fetch_pending_channel_messages(supabase) == []


def test_fetch_pending_channel_messages_filters_query():
    calls = []
    chain = _FakeChain([], calls)
    fake = MagicMock()
    fake.table.return_value = chain
    fetch_pending_channel_messages(fake)
    # dict() collapses repeated keys — count eq calls explicitly instead.
    eqs = [args for name, args in calls if name == "eq"]
    assert (
        "is_", ("danny_decision", "null")
    ) in calls
    assert ("in_", ("channel", ["email", "whatsapp", "call", "teams"])) in calls
    # Outgoing rows (the user's own sends) must NEVER surface — the feed
    # requires incoming direction even when a row forgot its terminal decision.
    assert ("direction", "incoming") in eqs
    assert ("classification", "actionable") in eqs


# ── fetch_pending_drafts ──────────────────────────────────────────

def test_fetch_pending_drafts_joins_message():
    supabase = _supabase_with([{
        "id": 109,
        "message_id": 7773,
        "draft_body": "Hi, thanks for your email.",
        "created_at": "2026-08-11T11:22:04+00:00",
        "messages": {"subject": "Re: PO", "sender_name": "Srikanth AR"},
    }])
    drafts = fetch_pending_drafts(supabase)
    assert len(drafts) == 1
    assert drafts[0]["id"] == 109
    assert drafts[0]["message_id"] == 7773
    assert drafts[0]["subject"] == "Re: PO"
    assert drafts[0]["sender_name"] == "Srikanth AR"
    assert "thanks" in drafts[0]["draft_body"]


def test_fetch_pending_drafts_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    assert fetch_pending_drafts(supabase) == []


# ── fetch_fyi_messages ────────────────────────────────────────────

def test_fetch_fyi_messages_shapes_rows():
    supabase = _supabase_with([
        {"id": 55, "channel": "email", "suggested_title": "Newsletter",
         "summary": "Vol 01", "sender_name": "AIS", "created_at": "2026-08-11T00:00:00+00:00"},
    ])
    items = fetch_fyi_messages(supabase)
    assert len(items) == 1
    assert items[0]["channel"] == "email"
    assert items[0]["title"] == "Newsletter"
    assert items[0]["summary"] == "Vol 01"
    assert items[0]["sender_name"] == "AIS"


def test_fetch_fyi_messages_title_falls_back_to_body_then_summary():
    # FYI rows carry no suggested_title/subject — the old shape showed
    # "Untitled" on every FYI card. Title must fall back to the message
    # body, then the classifier's summary, before giving up.
    supabase = _supabase_with([
        {"id": 1, "channel": "teams", "suggested_title": None,
         "subject": None, "body": "  Wow.. So, on track..  ",
         "summary": "Yashwant Daniel acknowledged the project is on track",
         "created_at": "2026-08-11T00:00:00+00:00"},
        {"id": 2, "channel": "whatsapp", "suggested_title": "",
         "subject": "", "body": "",
         "summary": "Mahi Rathi shared a reflection on her AI voice note",
         "created_at": "2026-08-11T00:00:00+00:00"},
        {"id": 3, "channel": "email", "suggested_title": None,
         "subject": None, "body": "", "summary": "",
         "created_at": "2026-08-11T00:00:00+00:00"},
    ])
    items = fetch_fyi_messages(supabase)
    assert items[0]["title"] == "Wow.. So, on track.."
    assert items[1]["title"] == "Mahi Rathi shared a reflection on her AI voice note"
    assert items[2]["title"] == "Untitled"


def test_fetch_fyi_messages_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    assert fetch_fyi_messages(supabase) == []


def test_fetch_fyi_messages_filters_outgoing():
    # Regression: sent emails were surfacing as FYI because the FYI feed
    # only filtered danny_decision IS NULL, not direction — outgoing rows
    # with a missing decision leaked into the feed. The guard must be present.
    calls = []
    chain = _FakeChain([], calls)
    fake = MagicMock()
    fake.table.return_value = chain
    fetch_fyi_messages(fake)
    eqs = [args for name, args in calls if name == "eq"]
    assert ("direction", "incoming") in eqs
    assert ("classification", "fyi") in eqs
    assert ("is_", ("danny_decision", "null")) in calls
