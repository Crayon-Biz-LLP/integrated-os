"""Shared Inbox (Quick Confirmations) feed builders.

The Inbox feed previously read ONLY `raw_dumps` rows filtered to
`status='pending'` — a shape nothing ever writes for channel items — so
actionable emails / Teams / WhatsApp / calls (stored in `messages` with
`danny_decision IS NULL`) never appeared in the tab, even though
`process_email_pending_decision` / `process_channel_pending_decision`
already handle their approve/reject server-side.

These builders fetch the real pending sources and shape them into the
payload shape the app's inbox already parses:

  - pending channel decisions  -> `{id, content, source, status:'pending',
    message_type, ...}` (the app's `_parseMessages` maps source -> type)
  - pending email drafts       -> `{id, message_id, subject, sender_name,
    draft_body, created_at}` (new "Email Drafts" section)
  - FYI items                  -> `{id, channel, title, sender_name,
    summary, created_at}` (new "For your info" section)

All functions are tenant-scoped via the caller's `tenant_aware_client()`
and fail open to [] on any query error (a feed problem must never 500 the
Inbox). Pure, sync, unit-testable.
"""

PENDING_CHANNELS = ["email", "whatsapp", "call", "teams"]

# FYI items stay visible for this long before decision_pulse expires them.
FYI_MAX_AGE_DAYS = 14


def shape_channel_message(row: dict) -> dict:
    """Shape a `messages` actionable row into the app's pending_messages shape.

    `_parseMessages` (api_service.dart) reads `source`, `status` and
    `message_type`, and uses `id`/`content` for the card. The `messages.id`
    (bigint) is exactly what `/api/{channel}-action` expects on approve.
    """
    channel = row.get("channel") or "email"
    return {
        "id": row.get("id"),
        "content": row.get("suggested_title") or row.get("subject") or "Untitled",
        "source": channel,
        "status": "pending",
        "message_type": f"{channel}_action",
        "created_at": row.get("created_at") or row.get("received_at"),
        "sender": row.get("sender_name"),
        "metadata": row.get("metadata") or {},
    }


def fetch_pending_channel_messages(supabase, limit: int = 50) -> list[dict]:
    """Actionable, undecided channel messages (email/whatsapp/call/teams)."""
    try:
        res = (
            supabase.table("messages")
            .select(
                "id, channel, classification, suggested_title, subject, "
                "sender_name, created_at, received_at, metadata"
            )
            .is_("danny_decision", "null")
            .in_("channel", PENDING_CHANNELS)
            .eq("classification", "actionable")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        return []
    return [shape_channel_message(r) for r in (res.data or [])]


def fetch_pending_drafts(supabase, limit: int = 20) -> list[dict]:
    """Pending email reply drafts, joined to their source message."""
    try:
        res = (
            supabase.table("email_drafts")
            .select(
                "id, message_id, draft_body, created_at, "
                "messages(subject, suggested_title, sender_name)"
            )
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        return []
    out = []
    for d in res.data or []:
        msg = d.get("messages") or {}
        out.append({
            "id": d.get("id"),
            "message_id": d.get("message_id"),
            "subject": (msg.get("subject") or msg.get("suggested_title")
                        or "Email draft"),
            "sender_name": msg.get("sender_name"),
            "draft_body": (d.get("draft_body") or "")[:500],
            "created_at": d.get("created_at"),
        })
    return out


def fetch_fyi_messages(supabase, limit: int = 20) -> list[dict]:
    """Undecided FYI channel items — informational, not approvals."""
    try:
        res = (
            supabase.table("messages")
            .select(
                "id, channel, classification, suggested_title, subject, "
                "sender_name, summary, created_at, received_at"
            )
            .is_("danny_decision", "null")
            .in_("channel", PENDING_CHANNELS)
            .eq("classification", "fyi")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        return []
    out = []
    for r in res.data or []:
        out.append({
            "id": r.get("id"),
            "channel": r.get("channel") or "email",
            "title": r.get("suggested_title") or r.get("subject") or "Untitled",
            "sender_name": r.get("sender_name"),
            "summary": (r.get("summary") or "")[:300],
            "created_at": r.get("created_at") or r.get("received_at"),
        })
    return out
