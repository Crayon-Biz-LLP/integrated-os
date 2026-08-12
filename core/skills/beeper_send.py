"""Phase C — Beeper send path: Rhodey replies through Beeper.

For the first time Rhodey can EXECUTE, not just triage: given a user-approved
message and a chat, it:

  1. resolves the Matrix room id for the chat (inverse of the bridge's
     per-tenant room map — chat_key → room_id),
  2. sends the message via the public Matrix API
     (`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`),
  3. records the send as an outgoing message
     (`record_outgoing_message()` → the auto-resolve rule fires: any stale
     pending approvals in that chat become 'responded'),
  4. marks the chat awaiting-reply (`mark_chat_awaiting_reply()`) so a reply
     arriving hours later is linked to what Rhodey asked/sent.

Sends are USER-APPROVED by construction — the endpoint (`/api/beeper-send`)
is API-key gated and the app only calls it after the user taps approve.
Human-paced: one message per request, no batching, no bulk send.

Chat identity: the room map stores room_id → {chat_key, phone, is_whatsapp,
is_group}. Resolution inverts it, matching on BOTH the chat key and the
phone so name-keyed and phone-keyed rows both find their room.
"""

import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from core.lib.audit_logger import audit_log_sync
from core.services.db import tenant_aware_client
from core.skills.beeper_ingest import (
    HOMESERVER,
    load_room_map,
    resolve_beeper_token,
)

SEND_TIMEOUT = 15.0


def resolve_room_id(supabase, uid: str | None, chat_key: str) -> str | None:
    """Inverse room-map lookup: chat_key (name or phone) → Matrix room id.

    Matches chat_key OR the entry's stored phone, so a name-keyed send
    resolves the phone-keyed room and vice versa.
    """
    room_map = load_room_map(supabase, uid)
    for room_id, entry in room_map.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("chat_key") == chat_key:
            return room_id
        if chat_key and entry.get("phone") == chat_key:
            return room_id
    return None


async def send_whatsapp_message(
    chat_key: str,
    message: str,
    uid: str | None = None,
    mark_awaiting: bool = True,
) -> dict:
    """Send a user-approved WhatsApp message to a chat via Beeper Matrix.

    Args:
        chat_key: the ingest chat key (room name or WhatsApp phone).
        message: the message body to send.
        uid: tenant user id. None → legacy/channel tenant resolution.
        mark_awaiting: mark the chat awaiting-reply after sending (default
            True — the send is an ask/nudge/confirmation the user is waiting
            to hear back on).

    Returns:
        dict with keys: status ('sent'|'error'|'no_room'|'no_token'),
        room_id, event_id, message_id, resolved.
    """
    token = resolve_beeper_token(uid)
    if not token:
        return {"status": "no_token",
                "error": "no Beeper token configured (user_oauth_tokens or env)"}

    supabase = tenant_aware_client()
    room_id = resolve_room_id(supabase, uid, chat_key)
    if not room_id:
        return {"status": "no_room",
                "error": f"no Matrix room found for chat '{chat_key}' — "
                         f"the bridge must have seen this chat once first"}

    txn_id = uuid.uuid4().hex
    # Matrix room ids contain reserved path chars (`!`, `:`) — percent-encode
    # the segment per the Matrix spec (every client library does this; some
    # homeservers/proxies reject the raw form).
    encoded_room = quote(room_id, safe='')
    url = f"{HOMESERVER.rstrip('/')}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
    body = message[:4000]
    payload = {"msgtype": "m.text", "body": body}

    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                audit_log_sync("beeper_send", "ERROR",
                               f"send to {chat_key} failed: HTTP {resp.status_code}: {resp.text[:200]}")
                return {"status": "error", "error": f"HTTP {resp.status_code}",
                        "room_id": room_id, "response": resp.text[:200]}
            event_id = (resp.json() or {}).get("event_id")
    except Exception as e:
        audit_log_sync("beeper_send", "ERROR", f"send to {chat_key} failed: {e}")
        return {"status": "error", "error": str(e), "room_id": room_id}

    # ── After-send wiring ─────────────────────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    resolved = 0
    message_id = None

    # 1. Record the outgoing message (fires the auto-resolve rule: pending
    #    approvals in this chat become 'responded').
    try:
        from core.lib.ingest import record_outgoing_message
        # Record the SAME truncated body that was actually sent so the DB row
        # never diverges from what really landed in the chat.
        rec = await record_outgoing_message(
            chat_id=chat_key,
            source="whatsapp",
            body=body,
            received_at=now,
            tracking_id=event_id,
            summary=body[:500],
            metadata={"room_id": room_id, "matrix_sender": "rhodey",
                      "matrix_event_id": event_id, "via": "beeper_send"},
        )
        if rec.get("status") == "filed":
            message_id = rec.get("message_id")
            resolved = rec.get("resolved", 0)
    except Exception as e:
        audit_log_sync("beeper_send", "WARNING", f"outgoing record failed: {e}")

    # 2. Mark the chat awaiting-reply (closes the tracker gap — nothing
    #    called mark_chat_awaiting_reply until now). Only when a tenant is
    #    resolved: awaiting_reply.owner_id is a uuid column and the row is
    #    tenant-scoped, so an unscoped send (legacy shared key, uid None)
    #    must skip the tracker rather than fail.
    if mark_awaiting and uid:
        try:
            from core.services.awaiting_reply import mark_chat_awaiting_reply
            mark_chat_awaiting_reply(
                supabase, uid, chat_key, "whatsapp",
                question=message[:300], linked_message_id=message_id,
            )
        except Exception as e:
            audit_log_sync("beeper_send", "WARNING",
                           f"mark awaiting-reply failed: {e}")

    audit_log_sync("beeper_send", "INFO",
                   f"Sent via Beeper to {chat_key} (room {room_id[:20]}…, "
                   f"event {event_id}); auto-resolved {resolved} pending item(s)")
    return {
        "status": "sent",
        "room_id": room_id,
        "event_id": event_id,
        "message_id": message_id,
        "resolved": resolved,
    }
