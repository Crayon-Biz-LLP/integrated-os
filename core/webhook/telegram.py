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


def _dedupe_acked_creation_receipts(receipts: list[str], message_text: str) -> list[str]:
    """B1: render_acks is the single creation voice. Drop emoji creation
    receipts ("✅ Task created: X") whose label is already confirmed in the
    message; keep failures and mutations, which render_acks does not carry."""
    return [
        r for r in receipts
        if not (r.startswith("✅ Task created: ")
                and r.split(": ", 1)[1].strip() in message_text)
    ]


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

        receipts = _dedupe_acked_creation_receipts(render_actions(evidence), message_text)
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

async def send_conversational(
    chat_id: int,
    fallback_text: str,
    context_text: str = None,
    **kwargs
) -> bool:
    """Wraps hardcoded system messages in the Universal Voice Synthesizer.
    
    If the LLM is down, it instantly falls back to `fallback_text` ensuring zero downtime.
    """
    try:
        from core.prompts.voice import get_voice
        from core.llm.compat import call_llm_with_fallback
        import logging
        
        prompt = f"""{get_voice()}

You need to tell the user the following system message/error:
"{fallback_text}"
{f'Context of what user tried to do: "{context_text}"' if context_text else ''}

Rewrite this system message into a single, natural, conversational reply from Rhodey.
Rules:
- Be concise (1 sentence max).
- Speak naturally (contractions allowed).
- Do not lose the core meaning of the message (e.g. if it says "PDF only", make sure you say "PDF only").
- Do not output the original string directly, make it sound like a human.
"""
        resp = await call_llm_with_fallback(prompt)
        text = resp.text.strip()
        if text:
            return await send_telegram(chat_id, text, **kwargs)
    except Exception as e:
        import logging
        logging.warning(f"send_conversational failed, using fallback: {e}")
        pass
        
    return await send_telegram(chat_id, fallback_text, **kwargs)

async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """REMOVED — Telegram channel retired. Raises NotImplementedError."""
    raise NotImplementedError("Telegram channel retired — download_telegram_file removed")


async def answer_callback_query(callback_query_id: str, text: str = None):
    """REMOVED — Telegram channel retired. Raises NotImplementedError."""
    raise NotImplementedError("Telegram channel retired — answer_callback_query removed")


KEYBOARD = {}  # Removed — Telegram keyboards no longer needed
