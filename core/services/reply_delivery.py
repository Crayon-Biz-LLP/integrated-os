"""Telegram-independent outbound reply delivery.

The app's conversation history reads bot outputs from ``raw_dumps``, and its
push-driven refresh listens for FCM pushes. Historically both were fired as a
side effect of ``send_telegram()`` — meaning the app's reply path was coupled
to the Telegram send. This module extracts that delivery so the app receives
replies even when Telegram is absent (or eventually removed).

Design:
- ``deliver_outbound_reply()`` is the single delivery point for bot replies:
  persist to raw_dumps + fire FCM push. No Telegram imports.
- ``send_telegram()`` (core/webhook/telegram.py) calls this FIRST, then sends
  to Telegram as an *optional* extra channel. If Telegram creds are missing,
  the reply still reaches the app through this module.
"""
from core.lib.audit_logger import audit_log_sync


def _persist_to_raw_dumps(message_text: str) -> None:
    """Write the reply to raw_dumps so the app's conversation history sees it.

    This is what /api/conversation-history and /api/messages read. Runs
    synchronously (matching the rest of the Supabase hot path) and fails open —
    a persist failure must never break the reply itself.
    """
    try:
        from core.services.db import get_supabase
        supabase = get_supabase()
        supabase.table('raw_dumps').insert({
            'content': message_text[:3000],  # Cap at 3000 chars for DB
            'status': 'completed',
            'direction': 'outgoing',
            'sender': 'system',
            'message_type': 'response',
            'source': 'telegram_bot',
            'metadata': {'type': 'bot_response'},
        }).execute()
    except Exception as e:
        audit_log_sync("reply_delivery", "WARNING", f"raw_dumps persist failed: {e}")


async def deliver_outbound_reply(
    message_text: str,
    notify_push: bool = True,
) -> int:
    """Deliver a bot reply to the app — no Telegram involved.

    Steps:
    1. Persist to raw_dumps (the app's conversation-history source).
    2. Fire an FCM push so the app refreshes instantly (unless notify_push=False,
       which the pulse uses because it sends its own dedicated push).

    Args:
        message_text: The full reply text.
        notify_push: Whether to fire the FCM push.

    Returns:
        Number of devices pushed (0 if push skipped or failed).
    """
    _persist_to_raw_dumps(message_text)

    if not notify_push:
        return 0

    try:
        from core.services.push_notification import send_push_notification
        pushed = await send_push_notification(
            title="Rhodey",
            body=message_text[:120] + ("\u2026" if len(message_text) > 120 else ""),
            data={"type": "briefing"},
        )
        return pushed
    except Exception as e:
        audit_log_sync("push", "ERROR", f"Reply push failed: {e}")
        return 0
