"""Stage A — Deterministic message sieve (free, no LLM).

Runs before ANY LLM call on channel messages. Marks content-less and
automated messages as noise so they never consume classifier budget or
pollute the FYI backlog.

Pure functions, unit-testable. Returns a verdict dict:
  {"noise": bool, "reason": str | None}

Covered cases (WhatsApp-first; Teams inherits the same rules):
- Media-only bodies ("Sent a picture", "Sent a voice note", media URLs)
- Emoji-only / reaction-only (<=3 chars all-emoji, or single reaction tokens)
- Single-token acknowledgements ("ok", "k", "oh", "lol", "👍")
- Automated participants (Mention Mirror, bridge bots, sponsored)
- Automated senders (NOREPLY_PATTERNS-compatible names)
- Junk (timestamps from voice-note playback, lone numbers)

NOT covered here (deliberately): real-text asks and real-text chit-chat —
those go to the ask-detector (Stage B) / LLM (Stage C).
"""

import re

from core.lib.chat_split import is_automated_participant

# ── Media markers ────────────────────────────────────────────────────
_MEDIA_MARKERS = (
    "sent a picture",
    "sent a photo",
    "sent a video",
    "sent a voice note",
    "sent a sticker",
    "sent a file",
    "sent a gif",
    "sent an image",
    "sent a document",
    "sent a contact",
    "sent a location",
    "incoming call",
    "missed call",
    "liked your photo",
    "loved your photo",
    "reacted to",
)

# Media/file URL patterns (image/video/audio hosts)
_MEDIA_URL_RE = re.compile(
    r"https?://[^\s]*(?:imgur|giphy|tenor|youtu|wa\.me|photos\.app|drive\.google|"
    r"\.(?:png|jpe?g|gif|mp4|mp3|webp|pdf|mov|avi|wav|ogg))[^\s]*",
    re.IGNORECASE,
)

# ── Emoji / reaction rules ───────────────────────────────────────────
_EMOJI_RE = re.compile(
    "[" 
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F780-\U0001F7FF"   # geometric
    "\U0001F800-\U0001F8FF"   # supplemental arrows
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\U0001FA00-\U0001FA6F"   # chess symbols
    "\U0001FA70-\U0001FAFF"   # extended-A
    "\U00002702-\U000027B0"   # dingbats
    "\U0000FE0F\u200D"        # variation selectors + ZWJ
    "]", 
    re.UNICODE,
)

# Single-token reaction/acknowledgement words (case-insensitive)
_REACTION_TOKENS = {
    "ok", "k", "kk", "oh", "ohh", "lol", "haha", "hehe", "yep", "ya",
    "yeah", "yes", "no", "nope", "👍", "🙏", "😂", "😅", "❤️", "♥",
    "amen", "thanks", "thank you", "ty", "wow", "nice", "great", "done",
    "sure", "fine", "hi", "hello", "hey", "good morning", "good evening",
    "good night", "good afternoon", "morning", "evening",
    # Short reaction phrases (≤3 words, no ask intent)
    "oh wow", "oh ok", "ok bro", "ok sir", "ok noted", "ok thanks",
    "looks great", "looks good", "looks nice", "so nice", "too good",
    "very nice", "amazing", "awesome", "fantastic", "super", "love it",
    "that's great", "thats great", "that's nice", "thats nice",
    "no idea", "yes yes", "okay", "kk bro",
}

# ── Junk patterns ────────────────────────────────────────────────────
_JUNK_RE = re.compile(r"^-?\d{6,}(:-?\d+)?$")          # voice-note timestamps
_LONE_NUMBER_RE = re.compile(r"^\d{1,4}$")              # tiny standalone numbers
_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Automated sender names (mirrors NOREPLY_PATTERNS from whatsapp_ingest)
_AUTO_SENDER_MARKERS = (
    "noreply", "no-reply", "donotreply", "notification", "alert",
    "bot", "automated", "service", "bridge", "mirror", "system",
)

# Automated notification body markers (service/billing/delivery alerts)
_AUTO_BODY_MARKERS = (
    "service alert",
    "your bill is",
    "bill is overdue",
    "is scheduled for suspension",
    "your connection is up for renewal",
    "otp",
    "one time password",
    "delivery update",
    "payment alert",
    "transaction alert",
)


def _is_automated_body(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _AUTO_BODY_MARKERS)


def _is_emoji_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # ≤3 chars and everything is emoji/whitespace
    return len(stripped) <= 3 and bool(_EMOJI_RE.search(stripped))


# Individual words that can appear in reaction/ack phrases (for multi-word
# checks like "oh wow looks great 😃" — every word is a reaction word).
_REACTION_WORDS = {
    "ok", "k", "kk", "oh", "ohh", "wow", "looks", "look", "great",
    "good", "nice", "super", "amazing", "awesome", "fantastic", "fine",
    "yes", "no", "yep", "ya", "yeah", "nope", "haha", "hehe", "lol",
    "amen", "thanks", "thank", "ty", "done", "sure", "hi", "hello",
    "hey", "morning", "evening", "noted", "idea", "bro",
    "sir", "love", "so", "too", "very", "that's", "thats", "it",
    "ohh", "okay", "kkk", "are", "is", "am",
}


def _is_reaction_token(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    if stripped in _REACTION_TOKENS:
        return True
    # multi-word phrase: every non-emoji word must be a reaction word and the
    # whole thing must be short (e.g. "oh wow looks great 😃", "ok bro 👍")
    tokens = stripped.split()
    if len(tokens) <= 5:
        core_tokens = [t for t in tokens if not _EMOJI_RE.search(t)]
        if not core_tokens:
            return True
        # punctuation-clean each word (e.g. "great😃" splits emoji off already)
        cleaned = [t.strip(".,!?~-") for t in core_tokens]
        if len(cleaned) <= 4 and all(t in _REACTION_WORDS for t in cleaned):
            return len(stripped) <= 30
    return False


def classify_sieve(
    body: str | None,
    sender_name: str | None = None,
    participant: str | None = None,
) -> dict:
    """Run the deterministic sieve over a message.

    Args:
        body: the raw message text
        sender_name: the sender display name (for automated-sender detection)
        participant: the group participant (Stage 0 split) for automated-
            participant detection

    Returns: {"noise": bool, "reason": str | None}
    """
    text = (body or "").strip()
    if not text:
        return {"noise": True, "reason": "empty_body"}

    # Automated participant (Mention Mirror / bridge bots inside groups)
    if is_automated_participant(participant):
        return {"noise": True, "reason": "automated_participant"}

    # Automated sender name
    if sender_name:
        low_name = sender_name.lower()
        if any(m in low_name for m in _AUTO_SENDER_MARKERS):
            return {"noise": True, "reason": "automated_sender"}

    # Automated notification bodies (billing/service alerts regardless of name)
    if _is_automated_body(text):
        return {"noise": True, "reason": "automated_notification"}

    # Media-only
    low = text.lower()
    if any(marker in low for marker in _MEDIA_MARKERS) and not _has_real_text(text):
        return {"noise": True, "reason": "media_only"}

    # Media URL only
    if _URL_ONLY_RE.match(text) or _MEDIA_URL_RE.match(text):
        return {"noise": True, "reason": "media_url"}

    # Emoji-only
    if _is_emoji_only(text):
        return {"noise": True, "reason": "emoji_only"}

    # Reaction / single-token ack
    if _is_reaction_token(text):
        return {"noise": True, "reason": "reaction_token"}

    # Junk
    if _JUNK_RE.match(text) or _LONE_NUMBER_RE.match(text):
        return {"noise": True, "reason": "junk"}

    return {"noise": False, "reason": None}


def _has_real_text(text: str) -> bool:
    """True when a media-marked body also carries real words (e.g. a caption
    or a follow-up ask after the media marker)."""
    # Strip media markers (case-insensitive) and URLs; if meaningful words
    # remain, keep the message for the ask-detector.
    stripped = text.lower()
    for marker in _MEDIA_MARKERS:
        stripped = stripped.replace(marker, "")
    stripped = re.sub(r"https?://\S+", "", stripped)
    words = re.findall(r"[a-z\u0900-\u097F]{3,}", stripped)
    return len(words) >= 2
