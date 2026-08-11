"""Stage 0 — Chat identity splitting for channel messages.

Live WhatsApp payloads stamp the chat identity onto EVERY message's
`sender_id` in two shapes:

  - Group:   "CirroCraft - Paulsons Ledgers: Nathan"
  - 1:1:     "Mohammed Yazir Crayon Employee"        (no colon)

The current pipeline treats `sender_id` as a stable chat key, which silently
breaks group batching (batch_whatsapp_message merges within ONE participant,
never across a group). This module parses the identity once at ingest and
persists the split so chat separation, group detection, and thread windows
are exact — not string heuristics.

Design notes:
- `split_chat_identity()` is pure and unit-testable.
- Group detection uses the colon boundary: groups are ALWAYS stamped
  "Chat Name: Participant". 1:1 chats are stamped with a bare name (no
  colon). A phone-number-looking sender_id is treated as 1:1 with the
  number as the chat key.
- The chat_id is the full prefix (trimmed); participant is the suffix.
- "Mention Mirror" / "Translator" / bridge bots are participants, NOT chats
  (they sit inside a real group) — the colon rule handles this naturally.
"""

import re

# Automated participants that sit inside real groups/chats and should never
# be treated as the chat itself. Also used by the sieve (Stage A) to drop
# their output without an LLM call.
AUTOMATED_PARTICIPANTS = (
    "mention mirror",
    "translator",
    "bridge bot",
    "whatsapp bridge",
    "sponsored",
    "system",
)

_PHONE_RE = re.compile(r"^\+?[\d\s\-]{8,}$")


def split_chat_identity(sender_id: str | None) -> dict:
    """Split a WhatsApp sender_id into (chat_id, participant, is_group).

    Returns a dict with keys: chat_id, participant (or None), is_group.
    Falls back gracefully: empty/None sender_id → 1:1 with chat_id=''.
    """
    raw = (sender_id or "").strip()
    if not raw:
        return {"chat_id": "", "participant": None, "is_group": False}

    # Phone numbers / identifiers with no name → 1:1 (chat key = the number)
    if _PHONE_RE.match(raw):
        return {"chat_id": raw, "participant": None, "is_group": False}

    if ":" in raw:
        chat_id, participant = raw.rsplit(":", 1)
        chat_id = chat_id.strip()
        participant = participant.strip() or None
        return {"chat_id": chat_id, "participant": participant, "is_group": True}

    return {"chat_id": raw, "participant": None, "is_group": False}


def is_automated_participant(participant: str | None) -> bool:
    """True when a group participant is an automated mirror/bridge/bot."""
    if not participant:
        return False
    p = participant.lower()
    return any(marker in p for marker in AUTOMATED_PARTICIPANTS)


def normalize_chat_key(sender_id: str | None) -> str:
    """Return the chat_id for a sender_id (used as the batching/thread key)."""
    return split_chat_identity(sender_id)["chat_id"]
