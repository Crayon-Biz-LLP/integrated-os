"""
Unit tests for core.services.awaiting_reply — the awaiting-reply tracker
and the auto-resolve rule (Phase A of the Beeper messaging layer).

Covered:
  - mark_chat_awaiting_reply: upsert one open ask per (owner, chat)
  - find_open_ask: returns only open asks
  - resolve_awaiting_reply: closes the ask, records replied_at
  - expire_stale_asks: TTL sweep
  - auto_resolve_on_outgoing: the stale-decision fix — pending items in
    the SAME chat received BEFORE the reply (within lookback) become
    danny_decision='responded'; items in OTHER chats, AFTER the reply, or
    older than the lookback window are untouched.
  - Fail-open behaviour: a DB error never propagates.
"""

import pytest


from unittest.mock import MagicMock

from core.services.awaiting_reply import (
    mark_chat_awaiting_reply,
    find_open_ask,
    resolve_awaiting_reply,
    expire_stale_asks,
    auto_resolve_on_outgoing,
)
pytestmark = pytest.mark.decision



class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeChain:
    """Minimal supabase query-builder chain that returns canned data and
    records the chain calls for query-shape assertions."""

    def __init__(self, data, calls=None):
        self._data = data
        self._calls = calls if calls is not None else []

    def _record(self, name, *args, **kwargs):
        self._calls.append((name, args, kwargs))
        return self

    def select(self, *a, **k):
        return self._record("select", *a, **k)

    def upsert(self, *a, **k):
        return self._record("upsert", *a, **k)

    def update(self, *a, **k):
        return self._record("update", *a, **k)

    def insert(self, *a, **k):
        return self._record("insert", *a, **k)

    def is_(self, *a, **k):
        return self._record("is_", *a, **k)

    def or_(self, *a, **k):
        return self._record("or_", *a, **k)

    def eq(self, *a, **k):
        return self._record("eq", *a, **k)

    def lt(self, *a, **k):
        return self._record("lt", *a, **k)

    def gte(self, *a, **k):
        return self._record("gte", *a, **k)

    def in_(self, *a, **k):
        return self._record("in_", *a, **k)

    def limit(self, *a, **k):
        return self._record("limit", *a, **k)

    def order(self, *a, **k):
        return self._record("order", *a, **k)

    def maybe_single(self):
        return self._record("maybe_single")

    def execute(self):
        return _FakeResponse(self._data)


def _supabase_with(data):
    """Fake supabase client whose .table('x') returns a _FakeChain."""
    fake = MagicMock()
    fake.table.return_value = _FakeChain(data)
    return fake


# ── mark_chat_awaiting_reply ─────────────────────────────────────────

def test_mark_chat_awaiting_reply_upserts_open_ask():
    data = [{"id": 1, "owner_id": "u1", "chat_id": "cnf", "status": "awaiting"}]
    supabase = _supabase_with(data)
    result = mark_chat_awaiting_reply(
        supabase, "u1", "cnf", "whatsapp", question="Ask Henry about CNF?"
    )
    assert result["status"] == "ok"
    assert result["data"]["chat_id"] == "cnf"

    chain = supabase.table.return_value
    joined = {c[0]: (c[1], c[2]) for c in chain._calls}
    upsert_args, upsert_kw = joined["upsert"]
    assert upsert_args[0]["owner_id"] == "u1"
    assert upsert_args[0]["chat_id"] == "cnf"
    assert upsert_args[0]["status"] == "awaiting"
    assert upsert_kw == {"on_conflict": "owner_id,chat_id"}


def test_mark_chat_awaiting_reply_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    result = mark_chat_awaiting_reply(supabase, "u1", "cnf", "whatsapp")
    assert result["status"] == "error"


# ── find_open_ask ────────────────────────────────────────────────────

def test_find_open_ask_returns_open_ask():
    data = {"id": 5, "chat_id": "cnf", "question": "Ask Henry about CNF?"}
    supabase = _supabase_with(data)
    ask = find_open_ask(supabase, "u1", "cnf")
    assert ask == data
    chain = supabase.table.return_value
    joined = {c[0]: (c[1], c[2]) for c in chain._calls}
    assert joined["eq"][0] == ("status", "awaiting")


def test_find_open_ask_none_when_closed():
    supabase = _supabase_with(None)
    assert find_open_ask(supabase, "u1", "cnf") is None


def test_find_open_ask_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    assert find_open_ask(supabase, "u1", "cnf") is None


# ── resolve_awaiting_reply ───────────────────────────────────────────

def test_resolve_awaiting_reply_marks_answered():
    supabase = _supabase_with([{"id": 5}])
    result = resolve_awaiting_reply(supabase, "u1", "cnf", "2026-08-12T10:07:00+00:00")
    assert result["status"] == "ok"
    assert result["resolved"] == 1
    chain = supabase.table.return_value
    joined = {c[0]: (c[1], c[2]) for c in chain._calls}
    assert joined["update"][0][0]["status"] == "answered"
    assert joined["update"][0][0]["replied_at"] == "2026-08-12T10:07:00+00:00"


def test_resolve_awaiting_reply_none_open():
    supabase = _supabase_with([])
    result = resolve_awaiting_reply(supabase, "u1", "cnf")
    assert result["status"] == "ok"
    assert result["resolved"] == 0


# ── expire_stale_asks ────────────────────────────────────────────────

def test_expire_stale_asks_marks_expired():
    supabase = _supabase_with([{"id": 3}])
    result = expire_stale_asks(supabase)
    assert result["status"] == "ok"
    assert result["expired"] == 1
    chain = supabase.table.return_value
    joined = {c[0]: (c[1], c[2]) for c in chain._calls}
    assert joined["update"][0][0]["status"] == "expired"
    assert joined["lt"][0] == ("expires_at", joined["lt"][0][1])


def test_expire_stale_asks_scopes_to_owner_when_given():
    supabase = _supabase_with([])
    expire_stale_asks(supabase, owner_id="u1")
    chain = supabase.table.return_value
    eqs = [c for c in chain._calls if c[0] == "eq"]
    assert ("owner_id", "u1") in [e[1] for e in eqs]


# ── auto_resolve_on_outgoing (the stale-decision fix) ────────────────

def test_auto_resolve_marks_same_chat_pending_as_responded():
    pending = [{"id": 101}, {"id": 102}]
    # fake chain returns pending ids on select, then [] for the resolve
    # update call's data
    supabase = _supabase_with(pending)
    result = auto_resolve_on_outgoing(
        supabase, "u1", "cnf", channel="whatsapp",
        replied_at="2026-08-12T10:07:00+00:00",
    )
    assert result["status"] == "ok"
    assert result["resolved"] == 2
    chain = supabase.table.return_value
    calls = chain._calls
    # The messages chain: select pending, then update them responded
    # (auto-resolve ALSO closes the awaiting_reply ask — filter to the
    # messages update by looking for the responded payload)
    update_calls = [c for c in calls if c[0] == "update"
                    and c[1] and c[1][0].get("danny_decision") == "responded"]
    assert len(update_calls) == 1
    assert update_calls[0][1][0]["decided_at"] == "2026-08-12T10:07:00+00:00"


def test_auto_resolve_queries_same_chat_only_before_reply_within_lookback():
    supabase = _supabase_with([])
    auto_resolve_on_outgoing(
        supabase, "u1", "cnf", channel="whatsapp",
        replied_at="2026-08-12T10:07:00+00:00",
    )
    chain = supabase.table.return_value
    joined = {c[0]: (c[1], c[2]) for c in chain._calls}
    # Chat identity OR: metadata->>chat_id OR sender_id (values quoted for
    # PostgREST or_ parsing — chat ids can contain special chars)
    or_expr = joined["or_"][0][0]
    assert 'metadata->>chat_id.eq."cnf"' in or_expr
    assert 'sender_id.eq."cnf"' in or_expr
    # Only pending items
    assert joined["is_"][0] == ("danny_decision", "null")
    # Same channel + incoming only (the rule must never resolve another
    # channel's chat or an outgoing row)
    eqs = [c[1] for c in chain._calls if c[0] == "eq"]
    assert ("channel", "whatsapp") in eqs
    assert ("direction", "incoming") in eqs
    # Received strictly before the reply
    assert joined["lt"][0] == ("received_at", "2026-08-12T10:07:00+00:00")
    # Within the lookback window
    assert joined["gte"][0][0] == "received_at"


def test_auto_resolve_escapes_quotes_in_chat_id():
    # Matrix-style room id with special characters
    supabase = _supabase_with([])
    auto_resolve_on_outgoing(
        supabase, "u1", '!abc:matrix.beeper.com"evil', channel="whatsapp"
    )
    chain = supabase.table.return_value
    or_expr = [c[1][0] for c in chain._calls if c[0] == "or_"][0]
    assert '"evil' not in or_expr  # double quotes stripped from value
    assert 'matrix.beeper.com' in or_expr


def test_auto_resolve_no_pending_items_returns_zero():
    supabase = _supabase_with([])
    result = auto_resolve_on_outgoing(supabase, "u1", "cnf")
    assert result["status"] == "ok"
    assert result["resolved"] == 0


def test_auto_resolve_fails_open():
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("boom")
    result = auto_resolve_on_outgoing(supabase, "u1", "cnf")
    assert result["status"] == "error"
