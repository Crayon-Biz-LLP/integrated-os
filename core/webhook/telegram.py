"""Reply delivery — app-only channel.

Formerly `send_telegram` (Telegram + app dual delivery). Now delivers
replies ONLY to the Flutter app via raw_dumps + FCM push.

All Telegram Bot API code, keyboards, file downloads, and callback
query handlers have been removed (Aug 27: Telegram channel retired).
"""

import re
from core.lib.audit_logger import audit_log_sync
from core.actions import (
    snapshot_action_context, validate_action_claims,
    render_actions, drain_action_context, capture_response,
)


async def deliver_reply(
    message_text: str,
    skip_validation: bool = False,
    notify_push: bool = True,
    intent: str = None,
    ack_title: str = None,
    persist_app: bool = True,
) -> bool:
    """Deliver a reply to the Flutter app (raw_dumps + FCM push).

    This is the single reply delivery path for the entire OS.
    All callers (handler.py, dispatch.py, api/index.py) use this.

    Returns True on success, False on failure.
    """
    try:
        evidence = snapshot_action_context()
        if not skip_validation:
            message_text, downgrades = validate_action_claims(message_text, evidence)
            if downgrades:
                audit_log_sync("actions", "HALLUCINATION_BLOCKED", {
                    "downgrade_count": len(downgrades),
                    "downgrade_categories": list(set(d["action_type"] for d in downgrades)),
                    "action_evidence_count": len(evidence),
                    "downgrades": downgrades
                })

        # Strip literal bracketed tags
        message_text = re.sub(r'\[(MEMORY|RESOURCE|TASK|PRACTICE)\]', '', message_text)
        # Strip common unbracketed trailing tags
        message_text = re.sub(r'\s+(MEMORY|RESOURCE|TASK|PRACTICE)(?=$|\n|[.,!?;:])', '', message_text)
        # Normalize excessive newlines (max 2 consecutive)
        message_text = re.sub(r'\n{3,}', '\n\n', message_text)
        # Clean up trailing spaces before newlines
        message_text = re.sub(r' +\n', '\n', message_text)

        receipts = render_actions(evidence)
        if receipts:
            receipts_text = "\n".join(receipts)
            if receipts_text.strip() not in message_text:
                message_text = f"{message_text}\n\n{receipts_text}"

        # Capture the final message text so the send-message endpoint can return it
        try:
            capture_response(message_text)
        except Exception:
            pass

        # Deliver to the APP — raw_dumps persist + FCM push
        from core.services.reply_delivery import deliver_outbound_reply
        await deliver_outbound_reply(
            message_text,
            notify_push=notify_push,
            intent=intent,
            ack_title=ack_title,
            persist_app=persist_app,
        )
        return True
    finally:
        drain_action_context()


# ── Backward compatibility alias ─────────────────────────────────────────────
# All existing callers use `send_telegram(chat_id, text, ...)`.
# The chat_id parameter is now ignored (Telegram retired).

async def send_telegram(
    chat_id: int,
    message_text: str,
    show_keyboard: bool = True,
    inline_keyboard: list = None,
    skip_validation: bool = False,
    notify_push: bool = True,
    intent: str = None,
    ack_title: str = None,
    persist_app: bool = True,
) -> bool:
    """Backward-compatible wrapper. chat_id is ignored (Telegram retired)."""
    return await deliver_reply(
        message_text,
        skip_validation=skip_validation,
        notify_push=notify_push,
        intent=intent,
        ack_title=ack_title,
        persist_app=persist_app,
    )


async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """REMOVED — Telegram channel retired. Raises NotImplementedError."""
    raise NotImplementedError("Telegram channel retired — download_telegram_file removed")


async def answer_callback_query(callback_query_id: str, text: str = None):
    """REMOVED — Telegram channel retired. Raises NotImplementedError."""
    raise NotImplementedError("Telegram channel retired — answer_callback_query removed")


KEYBOARD = {}  # Removed — Telegram keyboards no longer needed
