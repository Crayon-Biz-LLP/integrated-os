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


def _persist_to_raw_dumps(message_text: str, intent: str = None, ack_title: str = None) -> None:
    """Write the reply to raw_dumps so the app's conversation history sees it.

    This is what /api/conversation-history and /api/messages read. Runs
    synchronously (matching the rest of the Supabase hot path) and fails open —
    a persist failure must never break the reply itself.
    """
    try:
        # M3: tenant facade — raw_dumps.owner_id is NOT NULL (db/78), so an
        # unscoped insert would 400 in tenant mode (breaking the app's
        # conversation history) or land under the wrong owner. The facade
        # injects owner_id from the active tenant context.
        from core.services.db import tenant_aware_client
        supabase = tenant_aware_client()
        metadata = {'type': 'bot_response'}
        if intent:
            metadata['intent'] = intent
        if ack_title:
            metadata['title'] = ack_title
        supabase.table('raw_dumps').insert({
            'content': message_text[:3000],  # Cap at 3000 chars for DB
            'status': 'completed',
            'direction': 'outgoing',
            'sender': 'system',
            'message_type': 'response',
            'source': 'telegram_bot',
            'metadata': metadata,
        }).execute()
    except Exception as e:
        # In tenant mode, a TenantRequiredError means a caller forgot its
        # tenant scope — that's a real bug (the app's conversation history
        # would silently stop getting replies), not a transient infra
        # failure. Surface it as ERROR so it isn't hidden by fail-open.
        from core.services.db import tenant_mode_enabled, get_tenant
        if tenant_mode_enabled() and get_tenant() is None:
            audit_log_sync("reply_delivery", "ERROR",
                           f"raw_dumps persist failed — NO TENANT SCOPE (caller bug): {type(e).__name__}: {e}")
        else:
            audit_log_sync("reply_delivery", "WARNING", f"raw_dumps persist failed: {e}")


async def deliver_outbound_reply(
    message_text: str,
    notify_push: bool = True,
    intent: str = None,
    ack_title: str = None,
) -> int:
    """Deliver a bot reply to the app — no Telegram involved.

    Steps:
    1. Persist to raw_dumps (the app's conversation-history source).
    2. Fire an FCM push so the app refreshes instantly (unless notify_push=False,
       which the pulse uses because it sends its own dedicated push).

    Args:
        message_text: The full reply text.
        notify_push: Whether to fire the FCM push.
        intent: Structured ack intent (e.g. TASK_RESCHEDULED) persisted in
            raw_dumps metadata so the app renders the right card without
            parsing the (voice-rendered) text.
        ack_title: The bare entity title (task/note/event name) for that card.

    Returns:
        Number of devices pushed (0 if push skipped or failed).
    """
    _persist_to_raw_dumps(message_text, intent=intent, ack_title=ack_title)

    if not notify_push:
        return 0

    try:
        from core.services.push_notification import send_push_notification
        from core.services.push_notification import push_data_content
        from core.services.persona import persona_guard_text

        preview = message_text[:120] + ("\u2026" if len(message_text) > 120 else "")
        # M18 Phase 2A: never-guard on the banner preview — if it touches a
        # persona never-topic the lock-screen shows a neutral banner. The
        # FULL reply still travels in data.content, so the app renders it
        # intact; nothing is ever hidden from the conversation itself.
        preview = persona_guard_text(
            preview, fallback="New message from Rhodey"
        )
        pushed = await send_push_notification(
            title="Rhodey",
            body=preview,
            data={"type": "briefing", "content": push_data_content(message_text)},
        )
        return pushed
    except Exception as e:
        audit_log_sync("push", "ERROR", f"Reply push failed: {e}")
        return 0
