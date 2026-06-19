import re
from typing import Optional

# Common contractions and shorthand expansions for Rhodey-specific language
_SHORTHAND_MAP = {
    "ppl": "people",
    "info": "information",
    "thx": "thanks",
    "pls": "please",
    "btw": "by the way",
    "fyi": "for your information",
    "tbd": "to be decided",
    "tbf": "to be fair",
    "imo": "in my opinion",
    "imho": "in my humble opinion",
    "asap": "as soon as possible",
    "wrt": "with respect to",
    "re": "regarding",
}

# Stop words to avoid creating phrase nodes from
_STOP_WORDS = {
    "a", "an", "the", "this", "that", "these", "those",
    "it", "its", "they", "them", "their",
    "is", "was", "were", "are", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might",
    "can", "shall", "need", "must",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "about", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under",
    "and", "but", "or", "nor", "not", "so", "yet", "if",
    "because", "as", "until", "while",
    "yes", "no", "very", "just", "then", "now",
    "here", "there", "when", "where", "how", "what", "which", "who", "whom",
}


def normalize_phrase(text: str) -> str:
    """Normalize a phrase for matching: lowercase, strip, remove punctuation noise."""
    t = text.strip().lower()
    t = re.sub(r'[^\w\s\'-]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def is_noise_phrase(text: str) -> bool:
    """Check if a phrase is too short, generic, or stop-word only."""
    t = text.strip().lower()
    if len(t) < 3:
        return True
    words = t.split()
    if not words:
        return True
    if all(w in _STOP_WORDS for w in words):
        return True
    return False


def expand_shorthand(text: str) -> str:
    """Expand common shorthand before normalization."""
    t = text.strip().lower()
    for abbr, full in _SHORTHAND_MAP.items():
        t = re.sub(r'\b' + re.escape(abbr) + r'\b', full, t)
    return t


def classify_node_type(text: str, known_entities: Optional[dict] = None) -> str:
    """Attempt to classify a phrase node type.
    
    Uses a known entities map {normalized_label: type} if provided.
    Falls back to heuristics.
    """
    t = text.strip().lower()

    if known_entities and t in known_entities:
        return known_entities[t]

    if re.match(r'^[A-Z][a-z]+(\s[A-Z][a-z]+)*$', text.strip()):
        return "entity"

    return "concept"
