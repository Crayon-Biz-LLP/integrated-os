"""
Unit tests for core.skills.beeper_desktop — the Phase B2 Desktop API bridge.

Covered (pure logic — no network):
  - resolve_desktop_token: BEEPER_DESKTOP_TOKEN wins, matrix fallback, None
  - resolve_chat_meta: whatsapp filter, chat_key from title, 1:1 vs group,
    senderID→phone map (lid-ids included), contact phone
  - should_visit_chat: cold-start window, activity-advanced visits
  - collect_new_messages: newest-first filtering + backward paging
  - _scoped_message_id: per-chat scoping so id sequences cannot collide
  - route_message: outgoing → record_outgoing_message, incoming →
    process_whatsapp_message, phones resolved via participants
  - process_chat: cursor advances only to the newest routed message
"""

import pytest


import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.skills.beeper_desktop import (
    resolve_desktop_token,
    resolve_chat_meta,
    should_visit_chat,
    collect_new_messages,
    _scoped_message_id,
    route_message,
    process_chat,
    _cursor_key,
    BeeperDesktopClient,
)
pytestmark = pytest.mark.ingest



# ── token resolution ───────────────────────────────────────────────────

def test_resolve_desktop_token_prefers_desktop_env(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_TOKEN", "bdapi_desktop")
    monkeypatch.setenv("BEEPER_MATRIX_TOKEN", "bdapi_matrix")
    assert resolve_desktop_token() == "bdapi_desktop"


def test_resolve_desktop_token_falls_back_to_matrix_env(monkeypatch):
    monkeypatch.delenv("BEEPER_DESKTOP_TOKEN", raising=False)
    monkeypatch.setenv("BEEPER_MATRIX_TOKEN", "bdapi_matrix")
    assert resolve_desktop_token() == "bdapi_matrix"


def test_resolve_desktop_token_none_when_absent(monkeypatch):
    monkeypatch.delenv("BEEPER_DESKTOP_TOKEN", raising=False)
    monkeypatch.delenv("BEEPER_MATRIX_TOKEN", raising=False)
    assert resolve_desktop_token() is None


def test_resolve_desktop_token_blank_is_none(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_TOKEN", "   ")
    monkeypatch.delenv("BEEPER_MATRIX_TOKEN", raising=False)
    assert resolve_desktop_token() is None


# ── chat identity ──────────────────────────────────────────────────────

def _participant(pid="@whatsapp_lid-210629625438257:beeper.local",
                 phone="+919840265591", name="Dr Mallika", is_self=False):
    return {"id": pid, "phoneNumber": phone, "fullName": name, "isSelf": is_self}


def _chat(account="whatsapp", chat_type="single", title="Dr Mallika",
          chat_id="!chat1:beeper.local", participants=None,
          last_activity="2026-08-13T11:55:15.265Z"):
    return {
        "id": chat_id,
        "accountID": account,
        "network": "WhatsApp",
        "title": title,
        "type": chat_type,
        "lastActivity": last_activity,
        "participants": {"items": participants or [
            _participant(), _participant("@danielyashwant:beeper.com",
                                         phone=None, name="danielyashwant",
                                         is_self=True)]},
    }


def test_resolve_chat_meta_whatsapp_only():
    assert resolve_chat_meta(_chat(account="telegram")) is None


def test_resolve_chat_meta_1to1():
    meta = resolve_chat_meta(_chat())
    assert meta["chat_key"] == "Dr Mallika"
    assert meta["is_group"] is False
    assert meta["phone"] == "+919840265591"
    # lid-style sender ids resolve through the phones map
    assert meta["phones"]["@whatsapp_lid-210629625438257:beeper.local"] == "+919840265591"
    assert meta["desktop_chat_id"] == "!chat1:beeper.local"


def test_resolve_chat_meta_group():
    chat = _chat(title="ACC Elders + Danny", chat_type="group",
                 participants=[
                     _participant(pid="@whatsapp_lid-111:beeper.local",
                                  phone="+919176322898", name="A"),
                     _participant(pid="@whatsapp_lid-222:beeper.local",
                                  phone="+919966582412", name="B"),
                     _participant("@danielyashwant:beeper.com", phone=None,
                                  name="danielyashwant", is_self=True),
                 ])
    meta = resolve_chat_meta(chat)
    assert meta["is_group"] is True
    assert meta["chat_key"] == "ACC Elders + Danny"
    assert meta["phone"] == "+919176322898"
    assert len(meta["phones"]) == 2


def test_resolve_chat_meta_title_fallback_to_id():
    meta = resolve_chat_meta(_chat(title="  "))
    assert meta["chat_key"] == "!chat1:beeper.local"


# ── visit filter ───────────────────────────────────────────────────────

def _cutoff():
    return datetime.now(timezone.utc) - timedelta(days=30)


def test_should_visit_cold_start_recent():
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert should_visit_chat(recent, None, _cutoff()) is True


def test_should_visit_cold_start_stale():
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    assert should_visit_chat(stale, None, _cutoff()) is False


def test_should_visit_no_activity_signal():
    assert should_visit_chat(None, None, _cutoff()) is False


def test_should_visit_activity_advanced():
    cursor = {"sortKey": 100, "lastActivity": "2026-08-10T10:00:00.000Z"}
    newer = "2026-08-13T11:55:15.265Z"
    assert should_visit_chat(newer, cursor, _cutoff()) is True


def test_should_visit_no_new_activity():
    cursor = {"sortKey": 100, "lastActivity": "2026-08-13T11:55:15.265Z"}
    same = "2026-08-13T11:55:15.265Z"
    assert should_visit_chat(same, cursor, _cutoff()) is False


def test_should_visit_cursor_without_activity_visits():
    cursor = {"sortKey": 100}
    assert should_visit_chat("2026-08-13T11:55:15.265Z", cursor, _cutoff()) is True


# ── message collection (backward paging) ───────────────────────────────

def _msg(mid, sort_key, is_sender=False, text="hello", sender_id=None):
    return {
        "id": mid, "sortKey": str(sort_key), "text": text,
        "isSender": is_sender, "timestamp": "2026-08-13T11:55:15.265Z",
        "senderID": sender_id or "@whatsapp_lid-210629625438257:beeper.local",
        "senderName": "Dr Mallika",
    }


def _page(items, has_more, oldest_cursor=None):
    return {"items": items, "hasMore": has_more, "oldestCursor": oldest_cursor}


async def _collect(messages_by_page, cursor_sort=0):
    """messages_by_page: list of pages (each newest-first)."""
    pages = list(messages_by_page)

    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def list_messages(self, chat_id, cursor=None):
            self.calls.append(cursor)
            return pages.pop(0) if pages else None

    client = _FakeClient()
    result = await collect_new_messages(client, "!c1:beeper.local", cursor_sort)
    return result, client.calls


def test_collect_single_page_newer_than_cursor():
    msgs = [_msg("5", 500), _msg("4", 400), _msg("3", 300)]  # newest-first
    result, _ = asyncio.run(_collect([_page(msgs, False)], cursor_sort=0))
    assert [m["id"] for m in result] == ["5", "4", "3"]


def test_collect_filters_older_than_cursor():
    msgs = [_msg("5", 500), _msg("4", 400), _msg("3", 300)]
    result, _ = asyncio.run(_collect([_page(msgs, False)], cursor_sort=400))
    assert [m["id"] for m in result] == ["5"]  # only sortKey 500 > 400


def test_collect_pages_backward_across_cursor():
    page1 = _page([_msg("8", 800), _msg("7", 700)], True, oldest_cursor="700")
    page2 = _page([_msg("6", 600), _msg("5", 500)], False)
    result, calls = asyncio.run(_collect([page1, page2], cursor_sort=550))
    assert [m["id"] for m in result] == ["8", "7", "6"]  # 500 ≤ 550 excluded
    assert calls[0] is None          # first page: no cursor
    assert calls[1] == "700"         # paged backward from the oldest item


def test_collect_stops_when_page_crosses_cursor():
    page1 = _page([_msg("8", 800), _msg("7", 700)], True, oldest_cursor="700")
    page2 = _page([_msg("6", 600), _msg("5", 500)], False)
    result, calls = asyncio.run(_collect([page1, page2], cursor_sort=750))
    # The newest page's oldest item (700) is already ≤ cursor (750) → every
    # older page is too → stop after the first page, nothing to page for.
    assert [m["id"] for m in result] == ["8"]
    assert calls == [None]


def test_collect_pages_until_has_more_false_even_when_cursor_low():
    # cursor 0 → everything newer; page2 is entirely newer than cursor so
    # paging continues until hasMore is False (full backward walk).
    page1 = _page([_msg("8", 800), _msg("7", 700)], True, oldest_cursor="700")
    page2 = _page([_msg("6", 600), _msg("5", 500)], False)
    result, calls = asyncio.run(_collect([page1, page2], cursor_sort=0))
    assert [m["id"] for m in result] == ["8", "7", "6", "5"]
    assert calls == [None, "700"]


def test_collect_stops_on_has_more_false():
    page = _page([_msg("3", 300), _msg("2", 200)], False)
    result, calls = asyncio.run(_collect([page], cursor_sort=0))
    assert [m["id"] for m in result] == ["3", "2"]
    assert calls == [None]  # no extra page fetched


def test_collect_cold_start_takes_newest_page_only():
    """Regression: cold start (no cursor) must NOT walk full history — the
    first live tick grounded through one chat's years of history and burned
    the whole per-tick budget. page_back=False → newest page only."""
    page1 = _page([_msg("8", 800), _msg("7", 700)], True, oldest_cursor="700")
    page2 = _page([_msg("6", 600), _msg("5", 500)], False)

    async def _collect_no_backpage():
        pages = [page1, page2]

        class _FakeClient:
            def __init__(self):
                self.calls = []

            async def list_messages(self, chat_id, cursor=None):
                self.calls.append(cursor)
                return pages.pop(0) if pages else None

        client = _FakeClient()
        result = await collect_new_messages(client, "!c1:beeper.local", 0,
                                            page_back=False)
        return result, client.calls

    result, calls = asyncio.run(_collect_no_backpage())
    assert [m["id"] for m in result] == ["8", "7"]
    assert calls == [None]  # never paged backward


# ── chat listing horizon ───────────────────────────────────────────────

def test_list_all_chats_stops_at_horizon():
    """Listing halts once a page's newest chat is older than the activity
    window (chats are sorted newest-first) — it must not walk thousands of
    stale chats every tick. One extra page is fetched to DISCOVER the
    staleness boundary, but its items are never kept."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def list_chats(self, cursor=None):
            self.calls.append(cursor)
            if cursor is None:
                return {"items": [
                    _chat(title="Recent 1", last_activity=recent),
                    _chat(title="Recent 2", last_activity=recent),
                ], "hasMore": True, "oldestCursor": "t1"}
            # second page: all stale → listing must stop
            return {"items": [
                _chat(title="Stale 1", last_activity=stale),
                _chat(title="Stale 2", last_activity=stale),
            ], "hasMore": True, "oldestCursor": "t2"}

    client = _FakeClient()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    out = asyncio.run(_list_all(client, cutoff))
    assert [c["title"] for c in out] == ["Recent 1", "Recent 2"]
    assert client.calls == [None, "t1"]  # fetched 2 pages, kept only page 1


async def _list_all(client, cutoff):
    # Call the REAL list_all_chats with the fake as self (only uses
    # self.list_chats + MAX_CHATS_TOTAL — no token/base access).
    return await BeeperDesktopClient.list_all_chats(client, stop_before=cutoff)


# ── error classification (23505 = duplicate, not error) ───────────────

def test_classify_duplicate_key_violation_as_duplicate():
    from core.skills.beeper_desktop import _classify_error

    status, reason = _classify_error(Exception(
        "duplicate key value violates unique constraint \"unique_channel_message\""))
    assert status == "duplicate"

    status, reason = _classify_error(Exception(
        "{'message': 'duplicate key value violates unique constraint', 'code': '23505'}"))
    assert status == "duplicate"


def test_classify_real_error_as_error():
    from core.skills.beeper_desktop import _classify_error

    status, reason = _classify_error(Exception("connection reset by peer"))
    assert status == "error"
    assert reason == "connection reset by peer"


def test_route_outgoing_constraint_violation_counts_duplicate():
    def _raise(**kwargs):
        raise Exception("{'message': 'duplicate key value violates unique constraint "
                        "\\\"unique_channel_message\\\"', 'code': '23505'}")

    with patch("core.lib.ingest.record_outgoing_message", new=_raise):
        status, reason = asyncio.run(_route(_msg("1", 1, is_sender=True)))
    assert status == "duplicate"


# ── cursor persistence must actually execute ──────────────────────────

def test_save_cursor_executes_upsert():
    """Regression: supabase-py builds upserts lazily — a bare
    core_config_upsert(...) without .execute() is a SILENT no-op (no error,
    no audit, zero rows ever written). The live tick proved it: messages
    routed fine but no cursor row ever landed. The save must execute."""
    from core.skills.beeper_desktop import _save_cursor, _cursor_key

    executed = []
    fake_builder = MagicMock()
    fake_builder.execute.side_effect = lambda: executed.append(True) or fake_builder

    def _fake_upsert(supabase, row):
        assert row["key"] == _cursor_key("!c1:beeper.local")
        return fake_builder

    warned = []
    supabase = MagicMock()
    with patch("core.skills.beeper_desktop.core_config_upsert", new=_fake_upsert), \
         patch("core.skills.beeper_desktop.audit_log_sync",
               side_effect=lambda *a, **k: warned.append(a)):
        _save_cursor(supabase, "!c1:beeper.local", {"sortKey": 500})
    assert executed == [True], "upsert must be executed, not just built"
    assert warned == [], f"no warnings expected, got {warned}"


def test_save_cursor_audits_on_failure():
    from core.skills.beeper_desktop import _save_cursor

    warned = []
    supabase = MagicMock()

    def _fake_upsert(supabase, row):
        raise RuntimeError("boom")

    with patch("core.skills.beeper_desktop.core_config_upsert", new=_fake_upsert), \
         patch("core.skills.beeper_desktop.audit_log_sync",
               side_effect=lambda *a, **k: warned.append(a)):
        _save_cursor(supabase, "!c1:beeper.local", {"sortKey": 500})
    assert warned, "failure must audit a WARNING"
    assert "desktop cursor save failed" in warned[0][2]# ── cursor loads must parse TEXT content ─────────────────────────────

def test_load_cursor_parses_string_content():
    """Regression: core_config.content is a TEXT column — PostgREST returns
    it as a JSON string, NOT a dict. The old isinstance(content, dict) check
    was always False, so cursors NEVER loaded: every tick re-fetched each
    chat's newest page and re-routed the same messages (the ~230
    "duplicates" per tick) and gap-free backfill never engaged."""
    import json
    from core.skills.beeper_desktop import _load_cursor, _cursor_key

    class _Res:
        data = {"content": json.dumps({"sortKey": 952787, "lastActivity": "2026-08-13T11:55:15Z"})}

    supabase = MagicMock()
    builder = MagicMock()
    builder.execute.return_value = _Res()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value = builder

    cursor = _load_cursor(supabase, _cursor_key("!c1:beeper.local"))
    assert cursor == {"sortKey": 952787, "lastActivity": "2026-08-13T11:55:15Z"}


def test_load_cursor_handles_missing_row():
    from core.skills.beeper_desktop import _load_cursor, _cursor_key

    class _Res:
        data = None

    supabase = MagicMock()
    builder = MagicMock()
    builder.execute.return_value = _Res()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value = builder

    assert _load_cursor(supabase, _cursor_key("!c1:beeper.local")) is None


def test_load_cursor_handles_garbage_content():
    from core.skills.beeper_desktop import _load_cursor, _cursor_key

    class _Res:
        data = {"content": "not-json{{{"}

    supabase = MagicMock()
    builder = MagicMock()
    builder.execute.return_value = _Res()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value = builder

    assert _load_cursor(supabase, _cursor_key("!c1:beeper.local")) is None


# ── scoped message ids ─────────────────────────────────────────────────
def test_scoped_message_id():
    assert _scoped_message_id("!c1:beeper.local", {"id": "390445"}) == "!c1:beeper.local:390445"


def test_scoped_message_id_missing():
    assert _scoped_message_id("!c1:beeper.local", {}) is None


# ── routing ────────────────────────────────────────────────────────────

def _wa_meta():
    return {
        "desktop_chat_id": "!c1:beeper.local",
        "chat_key": "Dr Mallika",
        "is_group": False,
        "phone": "+919840265591",
        "phones": {"@whatsapp_lid-210629625438257:beeper.local": "+919840265591"},
        "last_activity": "2026-08-13T11:55:15.265Z",
    }


async def _route(msg, meta=None):
    supabase = MagicMock()
    return await route_message(supabase, msg, meta or _wa_meta())


def test_route_outgoing_records_with_scoped_tracking_id():
    captured = {}

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return {"status": "filed"}

    msg = _msg("390445", 952787, is_sender=True, text="OK ok.",
               sender_id="@danielyashwant:beeper.com")
    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record):
        status, reason = asyncio.run(_route(msg))
    assert status == "outgoing"
    assert captured["chat_id"] == "Dr Mallika"
    assert captured["source"] == "whatsapp"
    assert captured["tracking_id"] == "!c1:beeper.local:390445"
    assert captured["metadata"]["phone"] == "+919840265591"


def test_route_outgoing_duplicate():
    async def _fake_record(**kwargs):
        return {"status": "duplicate"}
    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record):
        status, reason = asyncio.run(_route(_msg("1", 1, is_sender=True)))
    assert status == "duplicate"


def test_route_incoming_routes_to_classification_pipeline():
    captured = {}

    async def _fake_classify(**kwargs):
        captured.update(kwargs)
        return {"status": "actionable"}

    msg = _msg("390446", 952788, text="Can you call me?")
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message", new=_fake_classify):
        status, reason = asyncio.run(_route(msg))
    assert status == "incoming"
    assert captured["sender_name"] == "Dr Mallika"
    assert captured["sender_phone"] == "+919840265591"
    assert captured["chat_id"] == "Dr Mallika"
    assert captured["participant"] is None        # 1:1
    assert captured["event_id"] == "!c1:beeper.local:390446"


def test_route_group_incoming_passes_participant():
    captured = {}

    async def _fake_classify(**kwargs):
        captured.update(kwargs)
        return {"status": "fyi"}

    meta = _wa_meta()
    meta["is_group"] = True
    msg = _msg("9", 900, text="meeting at 3",
               sender_id="@whatsapp_lid-210629625438257:beeper.local")
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message", new=_fake_classify):
        status, reason = asyncio.run(_route(msg, meta))
    assert status == "incoming"
    assert captured["participant"] == "+919840265591"


def test_route_incoming_without_phone_ignored():
    msg = _msg("9", 900, sender_id="@unknown:beeper.local")
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message") as _m:
        status, reason = asyncio.run(_route(msg))
    assert status == "ignored"
    _m.assert_not_called()


def test_route_empty_text_ignored():
    msg = _msg("9", 900, text="   ")
    with patch("core.lib.ingest.record_outgoing_message") as _r, \
         patch("core.skills.whatsapp_ingest.process_whatsapp_message") as _m:
        status, reason = asyncio.run(_route(msg))
    assert status == "ignored"
    _r.assert_not_called()
    _m.assert_not_called()


# ── process_chat: cursor advancement ───────────────────────────────────

def test_cursor_key():
    assert _cursor_key("!c1:beeper.local") == "beeper_desktop_cursor:!c1:beeper.local"


async def _run_process_chat(messages, cursor=None, budget=None):
    """messages: newest-first; single page, hasMore False."""
    class _FakeClient:
        async def list_messages(self, chat_id, cursor=None):
            return {"items": messages, "hasMore": False, "oldestCursor": None}

    saved = {}

    def _fake_save(supabase, chat_id, content):  # real _save_cursor is sync
        saved[chat_id] = content

    summary = {"processed": 0, "outgoing": 0, "incoming": 0,
               "ignored": 0, "duplicate": 0, "errors": 0}
    supabase = MagicMock()

    async def _fake_record(**kwargs):
        return {"status": "filed"}

    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.skills.beeper_desktop._save_cursor", new=_fake_save), \
         patch("core.skills.beeper_desktop.MAX_MESSAGES_PER_TICK",
               budget if budget is not None else 500):
        await process_chat(supabase, _FakeClient(), _wa_meta(), cursor, summary)
    return summary, saved


def test_process_chat_advances_cursor_to_newest():
    msgs = [_msg("5", 500, is_sender=True), _msg("4", 400, is_sender=True)]
    summary, saved = asyncio.run(_run_process_chat(msgs))
    assert summary["outgoing"] == 2
    assert saved["!c1:beeper.local"]["sortKey"] == 500
    assert saved["!c1:beeper.local"]["lastActivity"] == "2026-08-13T11:55:15.265Z"


def test_process_chat_budget_cut_keeps_remainder_for_next_tick():
    msgs = [_msg("5", 500, is_sender=True), _msg("4", 400, is_sender=True),
            _msg("3", 300, is_sender=True)]
    # budget of 1 → only the OLDEST new message (300) is routed; cursor must
    # advance to 300 so the newer 400/500 are still picked up next tick.
    summary, saved = asyncio.run(_run_process_chat(msgs, budget=1))
    assert summary["outgoing"] == 1
    assert saved["!c1:beeper.local"]["sortKey"] == 300
