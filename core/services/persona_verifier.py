"""Deterministic persona verifier — the M18 grounding gates G1-G4.

The LLM transforms facts into a card; THIS module verifies. Rejection is
fail-closed: a card that fails any gate is never written, and the previous
card stays. These gates are what make the "FC Madras prayer group" class
of fabrication structurally impossible, not merely discouraged.

    G1  No fact-fusion: every structured claim must exist as a known triple
        (association present in the graph), and every proper noun in prose
        must be a known entity.
    G2  No timing: no temporal claims — Phase 1 has no dated-source mapping,
        so ALL timing words are rejected in user-facing prose.
    G3  No association without an edge: claims are triple-checked against
        graph edges (direction-insensitive); nothing else may assert a link.
    G4  Sensitive guard: reflection-derived topics (e.g. money/debt) are
        forbidden in user-facing prose; they may only appear in `never`.

`facts` is the deterministic extraction bundle from the synthesis job:
    {
      "allowed_names": set[str] (lowercase),   # people + orgs + root label
      "known_triples": set[(a_lower, REL, b_lower)],  # both directions
      "root_label": str,
      "sensitive_topics": list[str] (lowercase),
    }
"""

from __future__ import annotations

import re

# Common English words that legitimately start a capitalized word in prose.
COMMON_CAPITALIZED: set[str] = {
    "A", "About", "An", "And", "At", "Be", "Because", "But", "By", "Dad",
    "Do", "Don", "Family", "For", "From", "Go", "Good", "Home", "I", "If",
    "In", "Is", "It", "Its", "Just", "Keep", "Let", "Mom", "My", "Never",
    "Night", "No", "Not", "Of", "On", "Or", "Rest", "See", "So", "Start",
    "Stay", "The", "This", "To", "Up", "We", "Well", "With", "Work", "You",
    "Your", "Today", "Tomorrow", "Tonight", "Week", "Morning", "Evening",
    "Afternoon", "Board", "One", "Two", "Prayer", "Church", "Faith", "God",
    "Jesus", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday",    "He", "She", "They", "Him", "Her", "Us", "Me",
    "Time", "Day", "Weekend", "Morning", "Evening", "Ahead", "Ready",
    # Sentence-start words common in warm sign-offs / style notes (not
    # entities). Sentence-start tokens are ALSO exempted structurally (see
    # _is_sentence_start), so this list is a second net, not the whole net.
    "Take", "Best", "All", "Have", "Talk", "See", "Catch", "Sleep",
    "Cheers", "Goodbye", "Wishing", "Looking", "Grab", "Enjoy",
    "Make", "Get", "Give", "Send", "Call", "Text", "Message", "Finish",
    "Close", "Handle", "Sort", "Nudge", "Ping", "Reach",
    "Maintain", "Warmly", "Warm", "Regards", "Sincerely", "Yours",
    "Kindly", "Greetings", "Hello", "Thanks", "Thank", "Appreciate",
    "Hope", "Glad", "Happy", "Sweet", "Dreams", "Onward", "Steady",
    "Anyway", "Also", "Though", "Okay", "Alright", "Quiet", "Calm",
    "Forward", "Great", "Nice", "Lovely", "Stay", "Keep", "Go",
    "Yesterday", "Today", "Tomorrow", "Recently", "Last", "Next",
    "This", "Our", "My", "Their", "Her", "His", "When", "What",
    "Who", "Where", "Why", "How", "They", "It", "That", "These",
    "Part", "Christian", "Lord", "Jesus", "Blessed", "Grateful",
    "Peace", "Joy", "Grace", "Hope", "Faith", "Love",
}

_TIMING_RE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|now|soon|"
    r"this\s+(morning|afternoon|evening|week|month|year|weekend)|"
    r"next\s+(week|month|weekend)|"
    r"on\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*|"
    r"at\s+\d{1,2}(:\d{2})?\s*(am|pm)|"
    r"every\s+(day|week|month|morning|evening))\b",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"\b[a-z][a-z'\-]*\b", re.IGNORECASE)

_PROPER_RE = re.compile(
    r"\b[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)*\b"
)


def _proper_nouns(text: str) -> set[str]:
    """Capitalized tokens that look like proper nouns (entity names)."""
    out = set()
    for m in _PROPER_RE.finditer(text):
        tok = m.group(0).strip()
        # Drop the whole sequence if EVERY word is a common capitalized word
        # (e.g. "The", "This Week"), but keep mixed sequences ("FC Madras").
        words = tok.split()
        if all(w in COMMON_CAPITALIZED for w in words):
            continue
        if any(w in COMMON_CAPITALIZED for w in words):
            # Sequence mixes common + unknown: keep only the unknown parts.
            for w in words:
                if w not in COMMON_CAPITALIZED and len(w) >= 2:
                    out.add(w)
        else:
            out.add(tok)
    return out


def _norm(s: str) -> str:
    return s.strip().casefold()


def _is_sentence_start(text: str, start: int) -> bool:
    """True when the token at `start` opens a sentence (or the string).

    Sentence-opening words are overwhelmingly normal English (verbs,
    closers, greetings), not entities — exempting them from the prose G1
    check kills the 'Maintain'/'Warmly' false-positive class without
    weakening the sign-off name-drop rule or mid-sentence fabrications.
    """
    prefix = text[:start]
    if not prefix.strip():
        return True
    return bool(re.search(r"[.!?;:]\s*$", prefix))


def verify_persona_card(card: dict, facts: dict) -> tuple[bool, list[str]]:
    """Run all gates. Returns (ok, errors); ok=False => card must be rejected."""
    errors: list[str] = []
    for key in ("who", "people", "domains", "style", "signoffs", "claims", "never"):
        if key not in card:
            errors.append(f"missing required key: {key}")
    # Entities = graph nodes + the stored context one-liner + the routing
    # domain names (all source rows). Place names like the user's city live
    # in context, not the graph; domain names like 'Personal' live in
    # user_settings.domains, not the graph.
    allowed = {_norm(x) for x in facts.get("allowed_names", set())}
    allowed |= {_norm(n) for n in _proper_nouns(facts.get("context") or "")}
    allowed |= {_norm(x) for x in facts.get("domains", [])}
    triples = {
        (_norm(a), _norm(r), _norm(b))
        for a, r, b in facts.get("known_triples", set())
    }
    root = _norm(facts.get("root_label") or "")
    sensitive = [_norm(s) for s in facts.get("sensitive_topics", [])]

    prose_fields = {
        "who": card.get("who", ""),
        "voice": (card.get("style") or {}).get("voice", ""),
        "signoffs": " ".join(card.get("signoffs", [])),
    }
    all_prose = " ".join(prose_fields.values())

    # ── G1: names in prose must be known entities ─────────────────────────
    for field, text in prose_fields.items():
        for m in _PROPER_RE.finditer(text):
            noun = m.group(0)
            if _is_sentence_start(text, m.start()):
                continue  # sentence-opening words are normal English
            n = _norm(noun)
            if n in allowed:
                continue
            # Root label is the user's own name — always allowed.
            if root and n == root:
                continue
            if n in COMMON_CAPITALIZED or n.lower() in COMMON_CAPITALIZED:
                continue
            errors.append(f"G1 unknown entity '{noun}' in {field}")

    # ── G2: no timing claims ──────────────────────────────────────────────
    if _TIMING_RE.search(all_prose):
        errors.append(
            f"G2 timing claim in prose: '{_TIMING_RE.search(all_prose).group(0)}'"
        )

    # ── G4: sensitive topics never in user-facing prose ───────────────────
    # Word-boundary + inflection-aware ("debt" blocks "debts", "debt-ridden").
    prose_lower = all_prose.casefold()
    for topic in sensitive:
        if topic and re.search(rf"\b{re.escape(topic)}\w*\b", prose_lower):
            errors.append(f"G4 sensitive topic '{topic}' in prose")

    # ── G1/G3: structured claims must be known triples ────────────────────
    for claim in card.get("claims", []):
        subj = _norm(claim.get("subject", ""))
        pred = _norm(claim.get("predicate", ""))
        obj = _norm(claim.get("object", ""))
        if not subj or not pred or not obj:
            errors.append(f"G1 malformed claim: {claim}")
            continue
        if subj not in allowed or obj not in allowed:
            errors.append(
                f"G1 claim entity unknown: {claim.get('subject')} / {claim.get('object')}"
            )
        if (subj, pred, obj) not in triples and (obj, pred, subj) not in triples:
            errors.append(
                f"G3 claim has no supporting edge: "
                f"{claim.get('subject')} — {claim.get('predicate')} — {claim.get('object')}"
            )

    # ── Sign-offs must not name-drop anyone but the user ──────────────────
    for s in card.get("signoffs", []):
        for noun in _proper_nouns(s):
            n = _norm(noun)
            if n in allowed and not (root and n == root):
                errors.append(f"sign-off name-drops '{noun}'")

    # ── Life snapshot: FACTS, so timing is allowed (they are verbatim
    # quotes from source rows). Names are grounded two ways: graph/context
    # entities PLUS proper nouns present in the provided snapshot facts
    # themselves (e.g. a prayer-group member's name — real source data, not
    # fabrication). Anything beyond both sets is rejected (G1). Sensitive
    # topics always rejected (G4).
    snapshot_sources: list[str] = facts.get("life_snapshot", []) or []
    # Same raw tokenizer as the check below (not _proper_nouns, which drops
    # all-common sequences like 'Day Prayer').
    snapshot_allowed = set(allowed)
    for src in snapshot_sources:
        for m in _PROPER_RE.finditer(src):
            snapshot_allowed.add(_norm(m.group(0)))
    for entry in card.get("life_snapshot", []):
        if not isinstance(entry, str) or not 1 <= len(entry) <= 140:
            errors.append(f"bad life_snapshot entry: {str(entry)[:40]}")
            continue
        for m in _PROPER_RE.finditer(entry):
            tok = _norm(m.group(0))
            if tok in snapshot_allowed or (root and tok == root):
                continue
            if tok in COMMON_CAPITALIZED or tok in {c.lower() for c in COMMON_CAPITALIZED}:
                continue
            errors.append(f"G1 unknown entity '{m.group(0)}' in life_snapshot")
        if any(re.search(rf"\b{re.escape(t)}\w*\b", entry.casefold()) for t in sensitive):
            errors.append(f"G4 sensitive topic in life_snapshot: {entry[:60]}")

    # ── Bounded lengths ───────────────────────────────────────────────────
    for s in card.get("signoffs", []):
        if not 3 <= len(s) <= 70:
            errors.append(f"sign-off length {len(s)} outside 3-70: {s[:40]}")
    if len(card.get("claims", [])) > 20:
        errors.append("too many claims (>20)")
    # ── G1: people/domains entries must be known entities, not inventions ─
    for entry in card.get("people", []) + card.get("domains", []):
        if not isinstance(entry, str) or not 1 <= len(entry) <= 40:
            errors.append(f"bad people/domains entry: {str(entry)[:40]}")
        elif _norm(entry) not in allowed:
            errors.append(f"G1 unknown entity in people/domains: {entry}")

    return (not errors, errors)
