"""
Unit tests for core.skills.beeper_send — the Phase C send path.

Covered (no network — httpx is faked):
  - resolve_room_id: chat_key match, phone match, miss
  - send_whatsapp_message: no token / no room / success / HTTP error
  - after-send wiring: outgoing recorded with the Matrix event_id as
    tracking_id, mark_chat_awaiting_reply called (and skipped when uid is
    None or mark_awaiting=False)
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

from core.skills.beeper_send import (
    load_room_map,
    resolve_room_id,
    send_whatsapp_message,
)


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _FakeClient:
    """Async httpx client that records the POST and returns a canned response."""

    def __init__(self, resp=None):
        self.resp = resp or _FakeResp()
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.posted.append((url, json, headers))
        return self.resp


def _supabase_with_room_map(room_map):
    fake = MagicMock()
    chain = MagicMock()
    chain.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.return_value.data = {
        "content": room_map
    }
    fake.table.return_value = chain
    return fake


ROOM_MAP = {
    "!r1:beeper.local": {
        "chat_key": "Jonathan Crosby ACC",
        "phone": "919176322898",
        "is_whatsapp": True,
        "is_group": False,
    },
    "!g1:beeper.local": {
        "chat_key": "ACC Elders + Danny",
        "phone": None,
        "is_whatsapp": True,
        "is_group": True,
    },
}


# ── room resolution ────────────────────────────────────────────────────

def test_load_room_map_returns_map():
    supabase = _supabase_with_room_map(ROOM_MAP)
    assert load_room_map(supabase, "u1") == ROOM_MAP


def test_load_room_map_empty_on_failure():
    supabase = MagicMock()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.maybe_single.return_value.execute.side_effect = Exception("boom")
    assert load_room_map(supabase, "u1") == {}


def test_resolve_room_id_by_chat_key():
    supabase = _supabase_with_room_map(ROOM_MAP)
    assert resolve_room_id(supabase, "u1", "Jonathan Crosby ACC") == "!r1:beeper.local"


def test_resolve_room_id_by_phone():
    # Phone-keyed sends must resolve name-keyed rooms and vice versa
    supabase = _supabase_with_room_map(ROOM_MAP)
    assert resolve_room_id(supabase, "u1", "919176322898") == "!r1:beeper.local"


def test_resolve_room_id_miss():
    supabase = _supabase_with_room_map(ROOM_MAP)
    assert resolve_room_id(supabase, "u1", "Unknown Chat") is None


# ── send_whatsapp_message ──────────────────────────────────────────────

def test_send_no_token_returns_no_token():
    with patch("core.skills.beeper_send.resolve_beeper_token", return_value=None):
        result = asyncio.run(send_whatsapp_message("Jonathan Crosby ACC", "hi", uid="u1"))
    assert result["status"] == "no_token"


def test_send_no_room_returns_no_room():
    supabase = _supabase_with_room_map(ROOM_MAP)
    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"):
        result = asyncio.run(send_whatsapp_message("Unknown Chat", "hi", uid="u1"))
    assert result["status"] == "no_room"


def test_send_success_records_outgoing_and_marks_awaiting():
    supabase = _supabase_with_room_map(ROOM_MAP)
    captured = {}

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return {"status": "filed", "message_id": 42, "resolved": 2}

    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(body={"event_id": "$sent1"}))), \
         patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply") as mark:
        result = asyncio.run(send_whatsapp_message(
            "Jonathan Crosby ACC", "Yes, call me at 4", uid="u1"))

    assert result["status"] == "sent"
    assert result["event_id"] == "$sent1"
    assert result["room_id"] == "!r1:beeper.local"
    assert result["resolved"] == 2

    # After-send wiring
    assert captured["chat_id"] == "Jonathan Crosby ACC"
    assert captured["source"] == "whatsapp"
    assert captured["tracking_id"] == "$sent1"  # exact Matrix event id dedup
    assert captured["metadata"]["via"] == "beeper_send"
    mark.assert_called_once()
    args = mark.call_args[0]
    assert args[1] == "u1"          # owner_id
    assert args[2] == "Jonathan Crosby ACC"  # chat_id


def test_send_marks_room_metadata_in_payload():
    supabase = _supabase_with_room_map(ROOM_MAP)
    client = _FakeClient(_FakeResp(body={"event_id": "$e"}))

    async def _fake_record(**kwargs):
        return {"status": "filed", "message_id": 1, "resolved": 0}

    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: client), \
         patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply"):
        asyncio.run(send_whatsapp_message("919176322898", "Hi there", uid="u1"))

    assert len(client.posted) == 1
    url, payload, headers = client.posted[0]
    # Room ids contain reserved path chars — must be percent-encoded
    assert "/rooms/%21r1%3Abeeper.local/send/m.room.message/" in url
    assert payload == {"msgtype": "m.text", "body": "Hi there"}
    assert headers["Authorization"] == "Bearer tok"


def test_send_skips_awaiting_when_uid_none():
    """Legacy unscoped key (uid None): no tracker write — owner_id is a
    uuid column and the row is tenant-scoped."""
    supabase = _supabase_with_room_map(ROOM_MAP)

    async def _fake_record(**kwargs):
        return {"status": "filed", "message_id": 42, "resolved": 0}

    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(body={"event_id": "$e"}))), \
         patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply") as mark:
        result = asyncio.run(send_whatsapp_message("Jonathan Crosby ACC", "hi", uid=None))
    assert result["status"] == "sent"
    mark.assert_not_called()


def test_send_skips_awaiting_when_mark_awaiting_false():
    supabase = _supabase_with_room_map(ROOM_MAP)

    async def _fake_record(**kwargs):
        return {"status": "filed", "message_id": 42, "resolved": 0}

    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(body={"event_id": "$e"}))), \
         patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply") as mark:
        result = asyncio.run(send_whatsapp_message(
            "Jonathan Crosby ACC", "hi", uid="u1", mark_awaiting=False))
    assert result["status"] == "sent"
    mark.assert_not_called()


def test_send_http_error_returns_error():
    supabase = _supabase_with_room_map(ROOM_MAP)
    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(status=403, body={"errcode": "M_FORBIDDEN"}))):
        result = asyncio.run(send_whatsapp_message("Jonathan Crosby ACC", "hi", uid="u1"))
    assert result["status"] == "error"
    assert result["room_id"] == "!r1:beeper.local"


def test_send_records_truncated_body_matching_send():
    """The DB record must reflect the exact body that went to WhatsApp
    (payload caps at 4000 chars — the record must not diverge)."""
    supabase = _supabase_with_room_map(ROOM_MAP)
    captured = {}
    long_msg = "x" * 5000

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return {"status": "filed", "message_id": 1, "resolved": 0}

    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(body={"event_id": "$e"}))), \
         patch("core.lib.ingest.record_outgoing_message", new=_fake_record), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply"):
        asyncio.run(send_whatsapp_message("Jonathan Crosby ACC", long_msg, uid="u1"))
    assert captured["body"] == long_msg[:4000]
    assert len(captured["body"]) == 4000


def test_send_record_failure_still_returns_sent():
    """The send itself succeeded — an outgoing-record hiccup must not
    surface as a failed send (fail-open after-send wiring)."""
    supabase = _supabase_with_room_map(ROOM_MAP)
    with patch("core.skills.beeper_send.tenant_aware_client", return_value=supabase), \
         patch("core.skills.beeper_send.resolve_beeper_token", return_value="tok"), \
         patch("httpx.AsyncClient", new=lambda *a, **k: _FakeClient(_FakeResp(body={"event_id": "$e"}))), \
         patch("core.lib.ingest.record_outgoing_message", side_effect=Exception("db down")), \
         patch("core.services.awaiting_reply.mark_chat_awaiting_reply"):
        result = asyncio.run(send_whatsapp_message("Jonathan Crosby ACC", "hi", uid="u1"))
    assert result["status"] == "sent"
    assert result["message_id"] is None
