"""Awaiting-reply tracker + auto-resolve rule (Phase A, Beeper messaging layer).

Two halves:

1. **Tracker** — the OS records that the user asked something in a chat
   (`mark_chat_awaiting_reply`), exposes the open ask for context linking
   (`find_open_ask`), and closes it when a reply lands
   (`resolve_awaiting_reply`). This closes the "OS never knew what you
   asked" gap: replies arriving hours later can be tied to the original
   question instead of floating loose.

2. **Auto-resolve rule** — the correctness fix for the stale-decision
   bug. When an OUTGOING message is recorded in a chat (`direction =
   'outgoing'`), any pending decision items (danny_decision IS NULL) in
   THAT SAME CHAT that arrived BEFORE the reply (within a lookback window)
   are marked `danny_decision = 'responded'`. They stop being surfaced in
   Quick Confirmation / Decision Pulse — because the user already answered
   in the conversation. This is exactly the case where an item was
   approved/replied to directly in Beeper and the OS was wrongly still
   asking.

Chat identity: matches the same merge key the ingest pipeline uses
(`metadata->>'chat_id'`, falling back to `sender_id`) so 1:1 chats and
groups resolve consistently with how messages were stored.

All functions are sync, tenant-scoped via the caller's client, and fail
open — a tracker hiccup must never break message ingest.
"""

from datetime import datetime, timezone, timedelta

from core.lib.audit_logger import audit_log_sync

DEFAULT_TTL_HOURS = 48       # how long an open ask stays relevant
DEFAULT_LOOKBACK_HOURS = 48  # auto-resolve only items received within this window
DEFAULT_MAX_RESOLVE = 50     # cap items resolved per outgoing message

STATUS_AWAITING = "awaiting"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_chat_awaiting_reply(
    supabase,
    owner_id: str,
    chat_id: str,
    channel: str,
    question: str | None = None,
    linked_message_id: int | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict:
    """Record an open ask for a chat (upsert: one open ask per owner+chat).

    Insert-or-replace semantics: if an open ask already exists for this
    (owner, chat), it is updated in place with the new question — there is
    never more than one live ask per chat, and the unique partial index in
    db/96 enforces it.
    """
    try:
        now = _now_iso()
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        row = {
            "owner_id": owner_id,
            "chat_id": chat_id,
            "channel": channel,
            "question": question,
            "status": STATUS_AWAITING,
            "linked_message_id": linked_message_id,
            "asked_at": now,
            # Re-opening an ask on a chat that was answered/expired: clear
            # any stale reply timestamp so the row reads as freshly open.
            "replied_at": None,
            "expires_at": expires,
        }
        res = (
            supabase.table("awaiting_reply")
            .upsert(row, on_conflict="owner_id,chat_id")
            .execute()
        )
        return {"status": "ok", "data": (res.data or [{}])[0]}
    except Exception as e:
        audit_log_sync(
            "awaiting_reply", "WARNING",
            f"mark_chat_awaiting_reply failed ({channel}/{chat_id}): {e}",
        )
        return {"status": "error", "error": str(e)}


def find_open_ask(supabase, owner_id: str, chat_id: str) -> dict | None:
    """Return the open ask for a chat, or None."""
    try:
        res = (
            supabase.table("awaiting_reply")
            .select("id, chat_id, channel, question, linked_message_id, asked_at")
            .eq("owner_id", owner_id)
            .eq("chat_id", chat_id)
            .eq("status", STATUS_AWAITING)
            .limit(1)
            .maybe_single()
            .execute()
        )
        return res.data
    except Exception:
        return None


def resolve_awaiting_reply(
    supabase, owner_id: str, chat_id: str, replied_at: str | None = None
) -> dict:
    """Close the open ask for a chat — a reply has landed.

    Also marks any awaiting ask past its TTL as expired opportunistically
    (best-effort; a tracker scan runs separately for the sweep).
    """
    try:
        replied = replied_at or _now_iso()
        res = (
            supabase.table("awaiting_reply")
            .update({"status": STATUS_ANSWERED, "replied_at": replied})
            .eq("owner_id", owner_id)
            .eq("chat_id", chat_id)
            .eq("status", STATUS_AWAITING)
            .execute()
        )
        count = len(res.data or [])
        if count:
            audit_log_sync(
                "awaiting_reply", "INFO",
                f"Ask answered: chat {chat_id} resolved at {replied}",
            )
        return {"status": "ok", "resolved": count}
    except Exception as e:
        audit_log_sync(
            "awaiting_reply", "WARNING",
            f"resolve_awaiting_reply failed ({chat_id}): {e}",
        )
        return {"status": "error", "error": str(e)}


def expire_stale_asks(supabase, owner_id: str | None = None) -> dict:
    """Mark awaiting asks past their expires_at as expired.

    Pass a scoped owner_id to expire one tenant's asks. With owner_id=None
    the sweep still routes through the caller's client — under tenant mode
    that means it MUST be called inside a tenant scope (e.g. via
    run_tenant_fanout / channel_tenant_scope), otherwise the tenant facade
    fails closed and the sweep reports an error instead of running.
    """
    try:
        now = _now_iso()
        chain = (
            supabase.table("awaiting_reply")
            .update({"status": STATUS_EXPIRED, "updated_at": now})
            .eq("status", STATUS_AWAITING)
            .lt("expires_at", now)
        )
        if owner_id:
            chain = chain.eq("owner_id", owner_id)
        res = chain.execute()
        count = len(res.data or [])
        if count:
            audit_log_sync(
                "awaiting_reply", "INFO", f"Expired {count} stale awaiting-reply asks"
            )
        return {"status": "ok", "expired": count}
    except Exception as e:
        audit_log_sync(
            "awaiting_reply", "WARNING", f"expire_stale_asks failed: {e}"
        )
        return {"status": "error", "error": str(e)}


def auto_resolve_on_outgoing(
    supabase,
    owner_id: str,
    chat_id: str,
    channel: str | None = None,
    replied_at: str | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_resolve: int = DEFAULT_MAX_RESOLVE,
) -> dict:
    """Auto-resolve stale pending decisions when the user replied in a chat.

    The rule: find pending items (danny_decision IS NULL) in the SAME chat
    (metadata->>'chat_id' or sender_id, same channel, incoming) that
    arrived BEFORE the reply and within the lookback window, and mark them
    `danny_decision='responded'` with decided_at = the reply time. They
    vanish from every pending feed (`is_('danny_decision', 'null')`
    filters) without any query changes.

    channel: when provided, the rule is restricted to that channel —
    guards against a chat_id/sender_id collision across channels. Pass
    the source (e.g. 'whatsapp') that the outgoing message came from.

    Also closes any open awaiting-reply ask for the chat.

    Returns a summary dict; never raises (fail-open).
    """
    try:
        replied = replied_at or _now_iso()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        ).isoformat()

        # Escape for PostgREST or_ parsing: chat ids (e.g. Matrix room ids
        # like "!abc:matrix.beeper.com") may contain characters with special
        # meaning in filter syntax, so quote the values. Double quotes
        # inside the value itself are stripped — chat keys never contain
        # them.
        esc = chat_id.replace('"', "")

        # 1. Find pending items in this chat received before the reply.
        #    Chat identity mirrors ingest: metadata->>'chat_id' else
        #    sender_id, PLUS metadata->>'phone' (the Beeper bridge stamps
        #    the WhatsApp phone on every row — a phone-keyed reply resolves
        #    the same chat's pending items even when name-keyed rows and
        #    phone-keyed rows differ).
        chain = (
            supabase.table("messages")
            .select("id")
            .is_("danny_decision", "null")
            .or_(
                f'metadata->>chat_id.eq."{esc}",'
                f'sender_id.eq."{esc}",'
                f'metadata->>phone.eq."{esc}"'
            )
        )
        if channel:
            chain = chain.eq("channel", channel)
        # Outgoing rows are never pending (they carry a non-null decision);
        # the explicit filter makes the invariant self-documenting.
        chain = chain.eq("direction", "incoming")
        res = (
            chain.lt("received_at", replied)
            .gte("received_at", cutoff)
            .limit(max_resolve)
            .execute()
        )
        pending_ids = [r["id"] for r in (res.data or [])]
        if not pending_ids:
            # No stale items — still close the tracker ask if one was open.
            resolve_awaiting_reply(supabase, owner_id, chat_id, replied)
            return {"status": "ok", "resolved": 0}

        # 2. Mark them responded (terminal decision — never surfaced).
        upd = (
            supabase.table("messages")
            .update({"danny_decision": "responded", "decided_at": replied})
            .in_("id", pending_ids)
            .execute()
        )
        resolved = len(upd.data or [])
        audit_log_sync(
            "awaiting_reply", "INFO",
            f"Auto-resolved {resolved} pending item(s) in chat {chat_id} "
            f"— user replied at {replied}",
        )

        # 3. Close the tracker ask too.
        resolve_awaiting_reply(supabase, owner_id, chat_id, replied)
        return {"status": "ok", "resolved": resolved, "ids": pending_ids}
    except Exception as e:
        audit_log_sync(
            "awaiting_reply", "WARNING",
            f"auto_resolve_on_outgoing failed ({chat_id}): {e}",
        )
        return {"status": "error", "error": str(e)}
