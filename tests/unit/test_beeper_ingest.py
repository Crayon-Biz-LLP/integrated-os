"""
Unit tests for core.skills.beeper_ingest — the Phase B1 Matrix bridge.

Covered (pure logic — no network):
  - extract_room_name / extract_room_phone from state events
  - resolve_chat_key: room name wins over phone; phone fallback for
    unnamed 1:1 rooms; raw room id as last resort
  - is_whatsapp_room detection from the room creator
  - is_user_send: own messages only, notices/reactions excluded
  - event_body: strips edits/reactions
  - event_ts_iso: ms epoch → ISO
  - resolve_beeper_token: oauth row first, env fallback, None when absent
"""

import pytest


import asyncio
import os
from unittest.mock import MagicMock, patch

from core.skills.beeper_ingest import (
    extract_room_name,
    extract_room_phone,
    resolve_chat_key,
    is_whatsapp_room,
    is_group_room,
    is_user_send,
    event_body,
    event_ts_iso,
    event_sender_phone,
    resolve_beeper_token,
    process_sync_tick,
)
pytestmark = pytest.mark.ingest



class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeChain:
    """Minimal supabase chain for process_sync_tick tests.

    Behaves like a tiny key-value store on core_config: upsert writes into
    the shared storage dict (keyed by the row's 'key'), select reads it
    back — so room-map persistence across ticks is actually exercised.
    """

    def __init__(self, data, storage=None):
        self._data = data
        self._storage = storage if storage is not None else {}
        self.calls = []

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        return self

    def select(self, *a, **k):
        return self._rec("select", *a, **k)

    def eq(self, *a, **k):
        return self._rec("eq", *a, **k)

    def limit(self, *a, **k):
        return self._rec("limit", *a, **k)

    def maybe_single(self):
        return self._rec("maybe_single")

    def upsert(self, *a, **k):
        self._rec("upsert", *a, **k)
        row = a[0] if a else k.get("row", {})
        if isinstance(row, dict) and row.get("key"):
            self._storage[row["key"]] = row.get("content")
        return self

    def execute(self):
        # For core_config selects, return what was stored under the eq key.
        eqs = [c for c in self.calls if c[0] == "eq"]
        if self._storage and eqs:
            key = eqs[-1][1][1] if len(eqs[-1][1]) > 1 else None
            if key in self._storage:
                return _FakeResponse({"content": self._storage[key]})
        return _FakeResponse(self._data)


def _supabase_with(data, storage=None):
    fake = MagicMock()
    fake.table.return_value = _FakeChain(data, storage=storage)
    return fake

OWN = "@danielyashwant:beeper.com"

# ── room identity ──────────────────────────────────────────────────────

def _state(name=None, creator="@whatsapp_919176322898:beeper.local"):
    events = [{"type": "m.room.create", "sender": creator,
               "content": {"creator": creator}}]
    if name:
        events.append({"type": "m.room.name", "content": {"name": name}})
    return events


def test_extract_room_name_present():
    assert extract_room_name(_state(name="Jonathan Crosby ACC")) == "Jonathan Crosby ACC"


def test_extract_room_name_missing():
    assert extract_room_name(_state()) is None


def test_extract_room_phone():
    assert extract_room_phone(_state()) == "919176322898"


def test_extract_room_phone_none_for_non_whatsapp():
    events = [{"type": "m.room.create", "sender": "@telegrambot:beeper.local",
               "content": {"creator": "@telegrambot:beeper.local"}}]
    assert extract_room_phone(events) is None


def test_resolve_chat_key_prefers_name():
    state = _state(name="ACC Elders + Danny", creator="@whatsapp_919176322898:beeper.local")
    key, meta = resolve_chat_key("!room1:beeper.local", state)
    assert key == "ACC Elders + Danny"
    assert meta["room_id"] == "!room1:beeper.local"
    assert meta["phone"] == "919176322898"


def test_resolve_chat_key_phone_fallback_for_unnamed_1to1():
    key, meta = resolve_chat_key("!room2:beeper.local", _state())
    assert key == "919176322898"
    assert meta["phone"] == "919176322898"


def test_resolve_chat_key_raw_room_id_last_resort():
    events = [{"type": "m.room.create", "sender": "@somebot:beeper.local",
               "content": {"creator": "@somebot:beeper.local"}}]
    key, meta = resolve_chat_key("!room3:beeper.local", events)
    assert key == "!room3:beeper.local"
    assert meta["phone"] is None


# ── room detection ─────────────────────────────────────────────────────

def test_is_whatsapp_room_true():
    assert is_whatsapp_room(_state()) is True


def test_is_whatsapp_room_false_for_other_network():
    events = [{"type": "m.room.create", "sender": "@discordgobot:beeper.local",
               "content": {"creator": "@discordgobot:beeper.local"}}]
    assert is_whatsapp_room(events) is False


# ── user-send detection ────────────────────────────────────────────────

def _msg(sender=OWN, msgtype="m.text", body="hello", ts=1755000000000, event_id="e1"):
    return {
        "type": "m.room.message",
        "sender": sender,
        "event_id": event_id,
        "origin_server_ts": ts,
        "content": {"msgtype": msgtype, "body": body},
    }


def test_is_user_send_own_message():
    assert is_user_send(_msg(), OWN) is True


def test_is_user_send_other_sender_false():
    assert is_user_send(_msg(sender="@whatsapp_919176322898:beeper.local"), OWN) is False


def test_is_user_send_own_notice_counts():
    # A user's own message delivered as m.notice is still their send
    assert is_user_send(_msg(msgtype="m.notice"), OWN) is True


def test_is_user_send_non_message_event_false():
    ev = {"type": "m.room.member", "sender": OWN}
    assert is_user_send(ev, OWN) is False


def test_is_user_send_without_own_user_false():
    assert is_user_send(_msg(), None) is False


# ── body / timestamp parsing ───────────────────────────────────────────

def test_event_body_plain():
    assert event_body(_msg(body="Yes, call me at 4")) == "Yes, call me at 4"


def test_event_body_reaction_stripped():
    ev = _msg()
    ev["content"]["m.relates_to"] = {"rel_type": "m.annotation", "event_id": "x"}
    assert event_body(ev) == ""


def test_event_body_edit_stripped():
    ev = _msg()
    ev["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "x"}
    assert event_body(ev) == ""


def test_event_ts_iso():
    iso = event_ts_iso(_msg(ts=1755000000000))
    assert iso is not None
    assert iso.startswith("2025-08-12")  # sanity: ms epoch → ISO day


def test_event_ts_iso_missing():
    assert event_ts_iso({"type": "m.room.message", "content": {}}) is None


# ── token resolution ───────────────────────────────────────────────────

def test_resolve_beeper_token_oauth_row_wins():
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = {
        "refresh_token": "sessecret"
    }
    with patch("core.skills.beeper_ingest.tenant_aware_client", return_value=fake):
        assert resolve_beeper_token("u1") == "sessecret"


def test_resolve_beeper_token_oauth_missing_falls_back_to_env():
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = None
    with patch("core.skills.beeper_ingest.tenant_aware_client", return_value=fake), \
         patch.dict(os.environ, {"BEEPER_MATRIX_TOKEN": "envtok"}, clear=False):
        assert resolve_beeper_token("u1") == "envtok"


def test_resolve_beeper_token_none_when_absent():
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = None
    with patch("core.skills.beeper_ingest.tenant_aware_client", return_value=fake), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BEEPER_MATRIX_TOKEN", None)
        assert resolve_beeper_token("u1") is None


def test_resolve_beeper_token_no_row_is_clean_none_no_warning():
    """Regression: supabase-py's maybe_single().execute() returns None ITSELF
    (not an empty result) when no row matches. Pre-fix this crashed on .data
    and audited a WARNING every 60s per tenant — the exact audit spam that
    masked the beeper outage (bridge skipped every tick since deploy while
    the logs filled with 'token lookup failed'). Must be a clean None with no
    warning so a missing token resolves quietly to the env fallback."""
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value = None
    warned = []
    with patch("core.skills.beeper_ingest.tenant_aware_client", return_value=fake), \
         patch("core.skills.beeper_ingest.audit_log_sync",
               side_effect=lambda *a, **k: warned.append(a)), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BEEPER_MATRIX_TOKEN", None)
        assert resolve_beeper_token("u1") is None
    assert warned == [], f"no-row lookup must not audit warnings, got {warned}"


def test_resolve_beeper_token_no_row_falls_back_to_env():
    """Same real-world no-row shape: clean fallback to BEEPER_MATRIX_TOKEN
    with no crash and no warning (env is the production token source)."""
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value = None
    warned = []
    with patch("core.skills.beeper_ingest.tenant_aware_client", return_value=fake), \
         patch("core.skills.beeper_ingest.audit_log_sync",
               side_effect=lambda *a, **k: warned.append(a)), \
         patch.dict(os.environ, {"BEEPER_MATRIX_TOKEN": "envtok"}, clear=False):
        assert resolve_beeper_token("u1") == "envtok"
    assert warned == []


# ── process_sync_tick (incremental-sync identity persistence) ──────────

def _wa_room_state(phone="919176322898", name=None):
    events = [{"type": "m.room.create", "sender": f"@whatsapp_{phone}:beeper.local",
               "content": {"creator": f"@whatsapp_{phone}:beeper.local"}}]
    if name:
        events.append({"type": "m.room.name", "content": {"name": name}})
    return events


def _payload(room_id, state_events, timeline_events):
    return {"rooms": {"join": {room_id: {
        "state": {"events": state_events},
        "timeline": {"events": timeline_events},
    }}}}


def _outgoing_event(body="Yes, call me at 4", event_id="$ev1"):
    return {"type": "m.room.message", "sender": OWN, "event_id": event_id,
            "origin_server_ts": 1755000000000,
            "content": {"msgtype": "m.text", "body": body}}


async def _run_tick(payload, supabase, record_results=None):
    async def _fake_record(**kwargs):
        return record_results or {"status": "filed"}
    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record):
        return await process_sync_tick(supabase, None, payload, OWN, "u1")


def test_tick_records_outgoing_with_state_present():
    supabase = _supabase_with(None)  # no persisted map yet
    payload = _payload(
        "!r1:beeper.local",
        _wa_room_state(name="ACC Elders + Danny"),
        [_outgoing_event()],
    )
    captured = {}

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return {"status": "filed"}

    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record):
        summary = asyncio.run(process_sync_tick(supabase, None, payload, OWN, "u1"))
    assert summary["outgoing"] == 1
    assert captured["chat_id"] == "ACC Elders + Danny"
    assert captured["tracking_id"] == "$ev1"
    assert captured["metadata"]["phone"] == "919176322898"


def test_tick_uses_persisted_room_map_when_state_empty():
    # Second tick: room arrives with EMPTY state (steady-state /sync) —
    # identity must come from the persisted map, not be dropped.
    supabase = _supabase_with(None)
    # Pre-seed the map via the same core_config read path used by the tick:
    # simplest is to let the first tick persist it, then run the second tick.
    payload1 = _payload("!r1:beeper.local", _wa_room_state(name="Jonathan Crosby ACC"), [])
    asyncio.run(_run_tick(payload1, supabase))
    assert supabase.table.return_value.calls  # a save happened

    payload2 = _payload("!r1:beeper.local", [], [_outgoing_event(body="Will do!")])
    captured = {}

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return {"status": "filed"}

    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record):
        summary = asyncio.run(process_sync_tick(supabase, None, payload2, OWN, "u1"))
    assert summary["outgoing"] == 1
    assert captured["chat_id"] == "Jonathan Crosby ACC"


def test_tick_skips_non_whatsapp_rooms():
    supabase = _supabase_with(None)
    state = [{"type": "m.room.create", "sender": "@telegrambot:beeper.local",
              "content": {"creator": "@telegrambot:beeper.local"}}]
    payload = _payload("!tg1:beeper.local", state, [_outgoing_event()])
    summary = asyncio.run(_run_tick(payload, supabase))
    assert summary["rooms_seen"] == 0
    assert summary["outgoing"] == 0


def test_tick_dedupes_duplicate_send():
    supabase = _supabase_with(None)
    payload = _payload("!r1:beeper.local", _wa_room_state(), [_outgoing_event()])
    summary = asyncio.run(_run_tick(payload, supabase, record_results={"status": "duplicate"}))
    assert summary["outgoing"] == 0
    assert summary["skipped"] == 1


def test_tick_skips_reactions_and_edits():
    supabase = _supabase_with(None)
    react = _outgoing_event(event_id="$r1")
    react["content"]["m.relates_to"] = {"rel_type": "m.annotation", "event_id": "x"}
    edit = _outgoing_event(body="old", event_id="$e1")
    edit["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "y"}
    payload = _payload("!r1:beeper.local", _wa_room_state(), [react, edit])
    summary = asyncio.run(_run_tick(payload, supabase))
    assert summary["outgoing"] == 0
    assert summary["skipped"] == 2


# ── group detection ────────────────────────────────────────────────────

def _member_events(others):
    events = []
    for uid in others:
        events.append({"type": "m.room.member", "state_key": uid,
                       "content": {"membership": "join"}})
    return events


def test_is_group_room_two_or_more_humans():
    events = _member_events(["@whatsapp_919176322898:beeper.local",
                             "@whatsapp_919966582412:beeper.local"])
    assert is_group_room(events, OWN) is True


def test_is_group_room_single_human_is_1to1():
    events = _member_events(["@whatsapp_919176322898:beeper.local"])
    assert is_group_room(events, OWN) is False


def test_is_group_room_excludes_user_and_bot():
    events = _member_events(["@whatsapp_919176322898:beeper.local",
                             "@whatsappbot:beeper.local"])
    assert is_group_room(events, OWN) is False


def test_event_sender_phone_extraction():
    ev = {"sender": "@whatsapp_919176322898:beeper.local"}
    assert event_sender_phone(ev) == "919176322898"
    assert event_sender_phone({"sender": "@telegrambot:beeper.local"}) is None


# ── incoming routing (cutover) ─────────────────────────────────────────

def _incoming_event(body="2 issues on the CNF account", event_id="$in1",
                    sender="@whatsapp_919176322898:beeper.local"):
    return {"type": "m.room.message", "sender": sender, "event_id": event_id,
            "origin_server_ts": 1755000000000,
            "content": {"msgtype": "m.text", "body": body}}


def test_tick_routes_incoming_to_classification_pipeline():
    supabase = _supabase_with(None)
    captured = {}

    async def _fake_classify(**kwargs):
        captured.update(kwargs)
        return {"status": "actionable", "suggested_title": "CNF call"}

    async def _fake_record(**kwargs):
        return {"status": "filed"}

    payload = _payload("!r1:beeper.local", _wa_room_state(), [_incoming_event()])
    with patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.skills.whatsapp_ingest.process_whatsapp_message", new=_fake_classify):
        summary = asyncio.run(process_sync_tick(supabase, None, payload, OWN, "u1"))
    assert summary["incoming"] == 1
    assert captured["event_id"] == "$in1"
    assert captured["sender_phone"] == "919176322898"
    assert captured["chat_id"] == "919176322898"  # unnamed 1:1 → phone key
    assert captured["participant"] is None  # 1:1


def test_tick_routes_group_incoming_with_participant():
    supabase = _supabase_with(None)
    captured = {}

    async def _fake_classify(**kwargs):
        captured.update(kwargs)
        return {"status": "fyi"}

    state = _wa_room_state(name="ACC Elders + Danny")
    state += _member_events(["@whatsapp_919176322898:beeper.local",
                             "@whatsapp_919966582412:beeper.local"])
    payload = _payload("!g1:beeper.local", state, [_incoming_event()])
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message", new=_fake_classify):
        summary = asyncio.run(process_sync_tick(supabase, None, payload, OWN, "u1"))
    assert summary["incoming"] == 1
    assert captured["chat_id"] == "ACC Elders + Danny"
    assert captured["participant"] == "919176322898"
    # Named group: sender_name is the PARTICIPANT phone, not the group name
    assert captured["sender_name"] == "919176322898"


def test_tick_named_1to1_uses_contact_name_as_sender():
    supabase = _supabase_with(None)
    captured = {}

    async def _fake_classify(**kwargs):
        captured.update(kwargs)
        return {"status": "fyi"}

    state = _wa_room_state(name="Jonathan Crosby ACC")
    payload = _payload("!r1:beeper.local", state, [_incoming_event()])
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message", new=_fake_classify):
        summary = asyncio.run(process_sync_tick(supabase, None, payload, OWN, "u1"))
    assert summary["incoming"] == 1
    assert captured["sender_name"] == "Jonathan Crosby ACC"
    assert captured["participant"] is None


def test_tick_ignores_incoming_reactions_and_edits():
    supabase = _supabase_with(None)
    react = _incoming_event(event_id="$i1")
    react["content"]["m.relates_to"] = {"rel_type": "m.annotation", "event_id": "x"}
    payload = _payload("!r1:beeper.local", _wa_room_state(), [react])
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message") as _m:
        summary = asyncio.run(_run_tick(payload, supabase))
    assert summary["incoming"] == 0
    _m.assert_not_called()


def test_tick_incoming_without_sender_phone_ignored():
    supabase = _supabase_with(None)
    ev = _incoming_event(sender="@telegrambot:beeper.local")
    payload = _payload("!r1:beeper.local", _wa_room_state(), [ev])
    with patch("core.skills.whatsapp_ingest.process_whatsapp_message") as _m:
        summary = asyncio.run(_run_tick(payload, supabase))
    assert summary["incoming"] == 0
    _m.assert_not_called()
