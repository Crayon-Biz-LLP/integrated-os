"""Rhodey's voice — acknowledgement helpers for hardcoded lines.

``RHODEY_VOICE`` in ``core/prompts/voice.py`` governs LLM-generated lines.
These helpers apply the *same voice* to code paths that write Rhodey's
lines directly (executor acks, callback replies, error messages), so every
line sounds like the same person:

- first sentence answers the question
- contractions ("it's", "that's")
- no emoji shouting prefixes (✅/⚠️/🚨 are system symbols, not speech)
- no log-line phrasing ("items will reappear in the next Decision Pulse")

Usage::

    from core.lib.rhodey_voice import ack_done, ack_logged, fail

    await send_telegram(chat_id, ack_logged(titles))
    await send_telegram(chat_id, fail("Couldn't approve that pattern: {e}"))
"""


def _strip(message: str) -> str:
    """Remove trailing/leading emoji + markdown noise from a subject."""
    s = str(message or "").strip()
    for token in ("✅ ", "⚠️ ", "↩️ ", "⏳ ", "📝 ", "🗑️ ", "❌ ", "**", "*", "`"):
        s = s.strip(token)
    return s.strip()


def ack_done(subject: str) -> str:
    """'X' -> 'Done — X is off the board.'"""
    s = _strip(subject)
    return f"Done — {s} is off the board." if s else "Done."


def ack_logged(subject: str) -> str:
    """'X, Y' -> 'X, Y — logged.' (mirrors the voice spec's 'X is logged.')"""
    s = _strip(subject)
    return f"{s} — logged." if s else "Logged."


def ack_added(subject: str) -> str:
    """'X' -> 'Got it — X is on your list.'"""
    s = _strip(subject)
    return f"Got it — {s} is on your list." if s else "Got it."


def ack_approved(subject: str) -> str:
    s = _strip(subject)
    return f"'{s}' is approved." if s else "Approved."


def ack_rejected(subject: str) -> str:
    s = _strip(subject)
    return f"'{s}' is rejected." if s else "Rejected."


def ack_merged(label: str, target: str) -> str:
    src = _strip(label)
    dst = _strip(target)
    return f"{src} is merged into {dst} — edges moved over."


def ack_undone(count: int, what: str) -> str:
    return (
        f"Undid {count} {what} — they'll come back in the next "
        "Decision Pulse for a fresh look."
    )


def ack_verified(count: int) -> str:
    return f"Verified {count} auto-decisions — that pattern's getting stronger."


def ok(message: str) -> str:
    """Success line — replaces the '✅ ...' prefix pattern across callbacks."""
    s = _strip(message)
    return s if s else "Done."


def fail(message: str) -> str:
    """Failure line — replaces the '⚠️ ...' prefix pattern across callbacks."""
    s = _strip(message)
    return s if s else "That didn't go through — try again?"
