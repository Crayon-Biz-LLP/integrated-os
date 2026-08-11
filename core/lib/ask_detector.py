"""Stage B — Ask-detector (deterministic cost filter).

Decides whether a message warrants an LLM classification call at all.
Escalates only when the message LOOKS like a request/ask — the LLM then
judges borderline cases. Everything else is noise without touching Gemini.

This inverts the cost model: instead of triaging every message, the LLM
only sees the ask fraction. Pure functions, unit-testable.

Escalation signals (ANY match escalates):
- Ask verbs / request forms ("can you", "please", "book", "let me know"...)
- The user's name or any graph person name mentioned
- Question / request shape ("?", "if yes", "let's", "shall we")
- Urgency words ("urgent", "asap", "today", "tonight")
- Explicit "call me" / "text me" / "message me" asks
"""

import re

# ── Ask verbs / request forms (case-insensitive substring match) ─────
_ASK_PHRASES = (
    "can you", "could you", "would you", "will you", "please", "pls",
    "let me know", "need you", "want you to", "could we", "can we",
    "book", "remind", "schedule", "confirm", "check", "review",
    "call me", "call us", "text me", "message me", "ping me",
    "send me", "send us", "give me", "give us", "look into", "look at",
    "take a look", "share", "forward", "update me", "keep me posted",
    "possible today", "possible tomorrow", "free today", "free tomorrow",
    "what time", "what's the status", "whats the status", "any update",
    "status?", "progress", "deadline", "when will", "when can",
    "i need", "we need", "i want", "we want", "do it", "go ahead",
    "fix", "remove", "add", "create", "prepare", "arrange", "organize",
    "get back", "reply", "respond", "help me", "help us", "assist",
)

# ── Urgency words (multi-word only — bare "today"/"now" is too noisy;
#    "possible today" / "free today" are already ask-phrases) ───────────
_URGENCY_WORDS = (
    "urgent", "asap", "emergency", "right now", "immediately",
    "as soon as possible", "before end of", "eod", "eob",
)

# ── Question / request shapes ────────────────────────────────────────
_QUESTION_RE = re.compile(r"\?\s*$|\?\s+if yes|\bif yes\b|\blet'?s\b|\bshall we\b|\bwhen\b|\bwhere\b|\bhow about\b")

# ── Name mentions: the user + any graph person names ─────────────────
def _mentions_any_name(text: str, user_name: str | None, graph_names: list[str] | None) -> bool:
    low = text.lower()
    candidates = [n for n in ([user_name] + (graph_names or [])) if n and len(n) >= 2]
    for name in candidates:
        # match whole-ish words (avoid "am" matching inside "camera")
        for piece in name.lower().split():
            if len(piece) >= 3 and re.search(rf"\b{re.escape(piece)}\b", low):
                return True
            if len(piece) < 3 and piece in low.split():
                return True
    return False


def should_escalate(
    text: str | None,
    user_name: str | None = None,
    graph_names: list[str] | None = None,
) -> dict:
    """Decide whether this message needs an LLM classification call.

    Args:
        text: the message body
        user_name: the user's first name (e.g. "Danny") for mention detection
        graph_names: known person names from the knowledge graph (optional)

    Returns: {"escalate": bool, "signals": list[str]}
    """
    body = (text or "").strip()
    if not body:
        return {"escalate": False, "signals": []}

    low = body.lower()
    signals = []

    # Question/request shape
    if _QUESTION_RE.search(body):
        signals.append("question_shape")

    # Ask phrases
    for phrase in _ASK_PHRASES:
        if phrase in low:
            signals.append(f"ask_phrase:{phrase}")
            break  # one ask phrase is enough to note

    # Urgency
    for word in _URGENCY_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", low):
            signals.append(f"urgency:{word}")

    # Name mention
    if _mentions_any_name(body, user_name, graph_names):
        signals.append("name_mention")

    escalate = bool(signals)
    return {"escalate": escalate, "signals": signals[:4]}
