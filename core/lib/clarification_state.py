"""Database-backed helpers for pending graph clarifications and active sessions.

Replaces the in-memory pending_graph_clarifications dict (handler.py) and
active_sessions dict (graph.py) that get wiped on Vercel cold restart.

Uses the pending_graph_clarifications table with context_json to store
arbitrary session state. pending_type distinguishes:
  - 'node' / 'edge' — clarification flow (handler.py)
  - 'session' — NLP correction sessions (graph.py)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase


def get_active_clarification(chat_id: int, pending_type: str = "node") -> Optional[dict]:
    """Fetch the first active (non-expired, not resolved) clarification for a chat.

    Returns the row dict or None if none exists.
    """
    supabase = get_supabase()
    try:
        rows = supabase.table("pending_graph_clarifications") \
            .select("*") \
            .eq("chat_id", chat_id) \
            .eq("pending_type", pending_type) \
            .eq("status", "active") \
            .gt("expires_at", datetime.now(timezone.utc).isoformat()) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if rows.data:
            return rows.data[0]
    except Exception as e:
        audit_log_sync("clarification_state", "WARNING",
                       f"Failed to get clarification for chat {chat_id}: {e}")
    return None


def set_clarification(chat_id: int, pending_id: int, *,
                      pending_type: str = "node",
                      step: str = "awaiting_person_context",
                      label: str = "",
                      expires_minutes: int = 5,
                      context_json: Optional[dict] = None) -> Optional[int]:
    """Create a new pending clarification for a chat.

    Automatically expires any prior active clarifications for the same chat+type,
    then inserts a fresh one. Returns the new row id.
    """
    supabase = get_supabase()
    now = datetime.now(timezone.utc)
    try:
        # Expire any existing active clarifications for this chat+type
        supabase.table("pending_graph_clarifications") \
            .update({"status": "expired", "resolved_at": now.isoformat()}) \
            .eq("chat_id", chat_id) \
            .eq("pending_type", pending_type) \
            .eq("status", "active") \
            .execute()

        row = supabase.table("pending_graph_clarifications").insert({
            "chat_id": chat_id,
            "pending_id": pending_id,
            "pending_type": pending_type,
            "step": step,
            "label": label,
            "context_json": context_json or {},
            "expires_at": (now + timedelta(minutes=expires_minutes)).isoformat(),
            "status": "active",
        }).execute()
        return row.data[0]["id"] if row.data else None
    except Exception as e:
        audit_log_sync("clarification_state", "ERROR",
                       f"Failed to set clarification for chat {chat_id}: {e}")
        return None


def resolve_clarification(chat_id: int, pending_type: str = "node",
                          *, status: str = "resolved") -> bool:
    """Mark all active clarifications for this chat+type as resolved/expired.

    Returns True if any rows were updated.
    """
    supabase = get_supabase()
    try:
        res = supabase.table("pending_graph_clarifications") \
            .update({
                "status": status,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }) \
            .eq("chat_id", chat_id) \
            .eq("pending_type", pending_type) \
            .eq("status", "active") \
            .execute()
        return len(res.data or []) > 0
    except Exception as e:
        audit_log_sync("clarification_state", "WARNING",
                       f"Failed to resolve clarification for chat {chat_id}: {e}")
        return False


def has_active_clarification(chat_id: int, pending_type: str = "node") -> bool:
    """Quick check — returns True if an active clarification exists for this chat+type."""
    return get_active_clarification(chat_id, pending_type) is not None


# ── Session state helpers (NLP correction sessions) ──


def get_active_session(chat_id: int) -> Optional[dict]:
    """Fetch the active NLP correction session for a chat.

    Returns the full session dict (with actions, original_items_map)
    or None if no active session exists.

    Mirrors the old get_active_session() from graph.py but backed by DB.
    """
    row = get_active_clarification(chat_id, pending_type="session")
    if not row:
        return None
    ctx = row.get("context_json") or {}
    if isinstance(ctx, str):
        import json
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    expires_raw = row.get("expires_at")
    expires_dt = datetime.now(timezone.utc)
    if expires_raw:
        try:
            expires_dt = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
        except Exception:
            pass
    return {
        "actions": ctx.get("actions", []),
        "original_items_map": ctx.get("original_items_map", {}),
        "expires_at": expires_dt,
    }


def set_session_state(chat_id: int, actions: list,
                      original_items_map: dict,
                      expires_minutes: int = 5) -> Optional[int]:
    """Store an active NLP correction session.

    Stores the full session data (actions + original_items_map) in
    context_json for the 'session' type.
    """
    return set_clarification(
        chat_id,
        pending_id=0,  # dummy — session type doesn't use pending_id
        pending_type="session",
        step="collecting_actions",
        label="",
        expires_minutes=expires_minutes,
        context_json={
            "actions": actions,
            "original_items_map": original_items_map,
        },
    )


def clear_session(chat_id: int) -> bool:
    """Remove the active NLP correction session for a chat."""
    return resolve_clarification(chat_id, pending_type="session")


def cleanup_expired_clarifications() -> int:
    """Mark all expired clarifications as 'expired' via the DB RPC (or direct update).

    Returns number of rows cleaned up (or -1 if RPC not found).
    """
    supabase = get_supabase()
    try:
        # Try the RPC first
        res = supabase.rpc("cleanup_expired_clarifications").execute()
        return res.data if isinstance(res.data, int) else -1
    except Exception:
        pass
    # Fallback: manual update
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("pending_graph_clarifications") \
            .update({"status": "expired", "resolved_at": now}) \
            .eq("status", "active") \
            .lt("expires_at", now) \
            .execute()
        return len(res.data or [])
    except Exception as e:
        audit_log_sync("clarification_state", "WARNING",
                       f"Failed to cleanup expired clarifications: {e}")
        return 0
