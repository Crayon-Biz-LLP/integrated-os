"""Rhodey's voice — acknowledgement helpers for hardcoded lines.

``RHODEY_VOICE`` in ``core/prompts/voice.py`` governs LLM-generated lines.
These helpers apply the *same voice* to code paths that write Rhodey's
lines directly (executor acks, callback replies, error messages), so every
line sounds like the same person:

- first sentence answers the question
- contractions ("it's", "that's")
- no emoji shouting prefixes (✅/⚠️/🚨 are system symbols, not speech)
- no log-line phrasing ("items will reappear in the next Decision Pulse")

``render_acks()`` is the single verb table for executor acknowledgments:
operation × status → line. It is the one place that decides what Rhodey
claims happened, so a "✅" can only ever be rendered for a *committed*
result (fail-closed: an exception can never produce a success line). The
app's card parser (rhodey_app/lib/widgets/rich_card_content.dart) keys off
the same emoji + verb vocabulary — change the table and the parser together.

Usage::

    from core.lib.rhodey_voice import ExecutionResult, render_acks

    await send_telegram(chat_id, "\n".join(render_acks(results)))
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union, List


@dataclass
class ExecutionResult:
    """The fact of one action's execution — the ack renderer's input.

    The executor never writes ack text; it emits these facts and the verb
    table renders them. `status` is set by the same code that wrote/refused
    the DB change, so a success line is structurally unreachable for a
    failed or rolled-back action.
    """

    operation: str
    status: str = "committed"  # committed | failed | skipped | rolled_back
    target_id: Optional[Union[int, str]] = None
    title: Optional[str] = None
    values: dict = field(default_factory=dict)  # e.g. {"new_reminder_at": iso}
    error: Optional[str] = None


def human_date(iso_value: str) -> str:
    """Best-effort '2026-08-20T12:40:28+05:30' -> 'Aug 20, 2026'."""
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return str(iso_value)


# Structured ack intents for the app's card renderer. The app keys off this
# intent (plus the ack title) instead of parsing text — the text below is free
# to sound like Rhodey. Card mapping lives in
# rhodey_app/lib/widgets/rich_card_content.dart (resolveCardData).
ACK_INTENTS = {
    "create_task": "TASK_CREATED",
    "create_note": "NOTE_LOGGED",
    "create_event": "EVENT_SCHEDULED",
    "reschedule": "TASK_RESCHEDULED",
    "modify_recurring": "RECURRENCE_UPDATED",
    "update_metadata": "TASK_UPDATED",
    "delete_event": "EVENT_DELETED",
    "close_task": "TASK_CLOSED",
    "suppress_instance": "TASK_CLOSED",
    "cancel_recurring": "RECURRENCE_CANCELLED",
}


def render_acks(results: List[ExecutionResult]) -> List[str]:
    """One Rhodey-voiced line per committed result, keyed by operation.

    - Only ``committed`` results render a line (fail-closed: failed and
      rolled-back actions are never acknowledged as successes).
    - Phrasing follows the voice spec (core/prompts/voice.py): confirmations
      like "Got it — X is on your list.", "X is logged.", "Done." — short,
      direct, contractions, the concrete delta (the date) shown so the math
      is provable.
    - The app does NOT parse this text: it receives the structured intent
      (ACK_INTENTS) + title via the message row and renders cards from that.
    """
    committed = [r for r in results if r.status == "committed"]
    lines: List[str] = []

    def _title(r: ExecutionResult) -> str:
        return r.title or f"item {r.target_id}"

    # Creations
    for r in committed:
        if r.operation == "create_note":
            lines.append(f"{_title(r)} — logged.")
        elif r.operation == "create_task":
            reminder = (r.values or {}).get("reminder_at")
            if reminder:
                lines.append(f"Got it — {_title(r)} is on your list for {human_date(reminder)}.")
            else:
                lines.append(f"Got it — {_title(r)} is on your list.")
        elif r.operation == "create_event":
            at = (r.values or {}).get("reminder_at") or (r.values or {}).get("time")
            if at:
                lines.append(f"Added {_title(r)} to your calendar for {human_date(at)}.")
            else:
                lines.append(f"Added {_title(r)} to your calendar.")

    # Mutations
    for r in committed:
        if r.operation == "reschedule":
            new_time = (r.values or {}).get("new_reminder_at")
            if new_time:
                lines.append(f"Moved {_title(r)} to {human_date(new_time)}.")
            else:
                lines.append(f"Moved {_title(r)}.")
        elif r.operation == "modify_recurring":
            lines.append(f"Updated {_title(r)}'s schedule.")
        elif r.operation == "update_metadata":
            # values is the normalized DB patch ({priority, deadline})
            fields = [k for k in (r.values or {}) if k in ("priority", "deadline")]
            if fields:
                lines.append(f"Updated {_title(r)}'s {', '.join(fields)}.")
            else:
                lines.append(f"Updated {_title(r)}.")
        elif r.operation == "delete_event":
            if r.title:
                lines.append(f"Removed {_title(r)} from your calendar.")
            else:
                lines.append("Removed that calendar event.")

    # Closures
    closures = [r for r in committed
                if r.operation in ("close_task", "suppress_instance", "cancel_recurring")]
    if closures:
        for r in closures:
            if r.operation == "cancel_recurring":
                lines.append(f"Cancelled — {_title(r)} won't repeat anymore.")
        done = [r for r in closures if r.operation != "cancel_recurring"]
        if done:
            if len(done) == 1:
                lines.append(f"Done — {_title(done[0])} is off your plate.")
            else:
                titles = ", ".join(_title(r) for r in done)
                lines.append(f"Done — {titles} are off your plate.")

    return lines


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
    # The voice spec bans log-line phrasing ("...in the next Decision Pulse")
    # — speak like a person, not a system.
    return f"Undid {count} {what} — they're back on the table."


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
