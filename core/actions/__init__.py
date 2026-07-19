import contextvars
import re
import re as _re
from dataclasses import dataclass, field
from typing import Literal, Optional

ActionType = Literal["task_create", "task_update", "calendar_create", "memory_save", 
                     "workflow_propose", "draft_create", "reminder_set", "none"]
ActionStatus = Literal["executed", "queued", "proposed", "failed", "not_attempted"]

@dataclass
class ActionResult:
    action_type: ActionType = "none"
    status: ActionStatus = "not_attempted"
    entity_id: Optional[str | int] = None
    human_label: Optional[str] = None
    evidence: dict = field(default_factory=dict)

_action_results: contextvars.ContextVar[list[ActionResult]] = contextvars.ContextVar('action_results', default=[])
_captured_response: contextvars.ContextVar[str | None] = contextvars.ContextVar('captured_response', default=None)
_captured_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar('captured_session_id', default=None)

def begin_action_context():
    _action_results.set([])
    _captured_response.set(None)
    _captured_session_id.set(None)

def clear_action_context():
    _action_results.set([])
    _captured_response.set(None)
    _captured_session_id.set(None)

def capture_session_id(session_id: str):
    """Capture the current conversation session_id during webhook processing."""
    _captured_session_id.set(session_id)

def get_captured_session_id() -> str | None:
    """Retrieve the last captured session_id after webhook processing."""
    return _captured_session_id.get()

def capture_response(text: str):
    """Capture the final outgoing message text during webhook processing."""
    _captured_response.set(text)

def get_captured_response() -> str | None:
    """Retrieve the last captured response after webhook processing."""
    return _captured_response.get()

def accumulate_action(result: ActionResult):
    lst = _action_results.get()
    lst.append(result)
    _action_results.set(lst)

def snapshot_action_context() -> list[ActionResult]:
    return list(_action_results.get())

def drain_action_context() -> list[ActionResult]:
    lst = _action_results.get()
    _action_results.set([])
    return lst

def can_claim_action(result: ActionResult) -> bool:
    """Only executed actions with a real entity ID can be claimed as performed."""
    return result.status == "executed" and result.entity_id is not None

def any_executed(results: list[ActionResult], action_type: Optional[ActionType] = None) -> bool:
    return any(
        can_claim_action(r) and (action_type is None or r.action_type == action_type)
        for r in results
    )

def render_actions(results: list[ActionResult]) -> list[str]:
    """Deterministically convert action results to user-facing sentences."""
    lines = []
    for r in results:
        label = r.human_label or str(r.entity_id or "")
        if r.status == "executed":
            if r.action_type == "task_create":
                lines.append(f"✅ Task created: {label}")
            elif r.action_type == "task_update":
                lines.append(f"✅ Task updated: {label}")
            elif r.action_type == "calendar_create":
                lines.append(f"📅 Calendar entry added: {label}")
            elif r.action_type == "memory_save":
                lines.append(f"📝 Note saved: {label}")
            elif r.action_type == "draft_create":
                lines.append(f"✍️ Draft created: {label}")
        elif r.status == "failed":
            error = r.evidence.get("error", "unknown error")
            lines.append(f"⚠️ {r.action_type.replace('_', ' ').capitalize()} failed: {error}")
        elif r.status == "proposed":
            lines.append(f"💡 Can do: {label}" if label else "")
    return [ln for ln in lines if ln]

# Post-generation validator
# Substring lexicon for claim classification
CLAIM_LEXICON = {
    "task_create": ["added the task", "created the task", "added task", "created task", "task has been created", "put that on your list", "added to your list", "consider it handled", "task created", "task added"],
    "monitoring": ["will watch", "i'll watch", "will monitor", "i'll monitor", "will alert", "i'll alert", "notify you when", "keep an eye on", "remind you when"],
    "communication": ["will ping", "i'll ping", "will check with", "i'll check with", "will reach out", "i'll reach out", "will send", "i'll send", "will message", "i'll message", "will email", "i'll email", "will call", "i'll call"],
    "scheduling": ["have scheduled", "i've scheduled", "booked", "rescheduled", "cancelled the meeting", "postponed"],
    "task_update": ["updated the task", "task updated", "changed the task"]
}

REWRITE_MAP = {
    "task_create": "I can create a task for this",
    "monitoring": "I can set up a reminder to check this",
    "communication": "I can help follow up on this",
    "scheduling": "I can help schedule this",
    "task_update": "I can update the task"
}

def classify_claims(text: str) -> set[str]:
    """Detect what kinds of claims are being made in the text using phrase families."""
    text_lower = text.lower()
    claims = set()
    for category, phrases in CLAIM_LEXICON.items():
        if any(phrase in text_lower for phrase in phrases):
            claims.add(category)
    return claims

RESERVED_ACTION_PATTERNS = [
    (re.compile(r"\bI(?:'|\s+ha)ve\s+added\s+(?:the\s+)?task\b", re.I), "task_create"),
    (re.compile(r"\bI(?:'|\s+ha)ve\s+created\b", re.I), "task_create"),
    (re.compile(r"\b(?:task|it)\s+(?:has been|was)\s+created\b", re.I), "task_create"),
    (re.compile(r"\b(?:put that on your list|added to your list|consider it handled)\b", re.I), "task_create"),
    (re.compile(r"\bI(?:'| wi)ll\s+(watch|monitor|alert(?: you)?|notify|keep an eye on)\b", re.I), "monitoring"),
    (re.compile(r"\bI(?:'| wi)ll\s+(ping|check|reach\s+out)\b", re.I), "communication"),
    (re.compile(r"\bI(?:'| wi)ll\s+(send|message|call|email)\b", re.I), "communication"),
    (re.compile(r"\bI(?:'|\s+ha)ve\s+(scheduled|booked|rescheduled|cancelled|postponed)\b", re.I), "scheduling"),
    (re.compile(r"\bI(?:'|\s+ha)ve\s+(updated|changed)\s+(?:the\s+)?task\b", re.I), "task_update"),
]

def validate_action_claims(text: str, evidence: list[ActionResult]) -> tuple[str, list[dict]]:
    """
    Scan text for unbacked action claims using classifier then regex rewrite.
    Returns (cleaned_text, downgrade_events).
    """
    detected_claims = classify_claims(text)
    if not detected_claims:
        return text, []
        
    executed_types = {r.action_type for r in evidence if r.status == "executed"}
    unbacked_claims = detected_claims - executed_types
    
    if not unbacked_claims:
        return text, []

    downgrades = []
    
    # Apply regex rewrites for the unbacked claims
    for pattern, category in RESERVED_ACTION_PATTERNS:
        if category in unbacked_claims:
            matches = list(pattern.finditer(text))
            for m in reversed(matches):
                rewrite = REWRITE_MAP.get(category, "I can help with this")
                downgrades.append({
                    "pattern": pattern.pattern,
                    "original": m.group(0),
                    "rewrite_to": rewrite,
                    "action_type": category,
                })
                text = text[:m.start()] + rewrite + text[m.end():]
                
    return text, downgrades


# ──────────────────────────────────────────
# Factual claim validation (date hallucination guard)
# ──────────────────────────────────────────

# Match "15 July 2026", "July 15, 2026", "15th July", "Jul 15" — month+day patterns
_DATE_PATTERNS = [
    _re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(?:(\d{4}))?\b", _re.I),
    _re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(\d{4}))?\b", _re.I),
]


def _normalize_date_text(text: str) -> str:
    """Normalize date-like strings to a canonical form for comparison.
    E.g., '15 July 2026' and 'July 15, 2026' both become '15/july/2026'."""
    month_map = {
        'january': '1', 'february': '2', 'march': '3', 'april': '4',
        'may': '5', 'june': '6', 'july': '7', 'august': '8',
        'september': '9', 'october': '10', 'november': '11', 'december': '12',
        'jan': '1', 'feb': '2', 'mar': '3', 'apr': '4', 'jun': '6',
        'jul': '7', 'aug': '8', 'sep': '9', 'oct': '10', 'nov': '11', 'dec': '12'
    }
    text_lower = text.lower().strip().rstrip(',').rstrip('.')
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            groups = m.groups()
            # First group is day in pattern 1, month in pattern 2
            if groups[0].lower() in month_map:
                # Pattern 2: "July 15, 2026" — month first
                month = month_map[groups[0].lower()]
                day = groups[1]
                year = groups[2] or ''
            else:
                # Pattern 1: "15 July 2026" — day first
                day = groups[0]
                month = month_map.get(groups[1].lower(), '0')
                year = groups[2] or ''
            return f"{day}/{month}/{year}"
    return text_lower


def validate_factual_claims(response_text: str, context_str: str) -> tuple[str, list[dict]]:
    """
    Scan the LLM response for date references and verify each one exists
    in the context string. If an unbacked date is found, flag it.

    Returns (cleaned_text, hallucination_events) where hallucination_events
    are dicts with keys: date_mentioned, context_found (bool).
    """
    hallucination_events = []
    
    # Normalize context for comparison
    context_lower = context_str.lower()
    
    # Find all date-like patterns in the response
    seen_dates = set()
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(response_text):
            raw = m.group(0)
            if raw in seen_dates:
                continue
            seen_dates.add(raw)
            
            norm = _normalize_date_text(raw)
            
            # Check if this date (or parts of it) appears in the context
            date_in_context = norm.lower() in context_lower
            
            # Also check for the raw date string in context
            if not date_in_context:
                date_in_context = raw.rstrip(',').rstrip('.').lower() in context_lower
            
            # Also check for partial matches (e.g. "15 July" in context but year differs)
            if not date_in_context and '/' in norm:
                parts = norm.split('/')
                if len(parts) >= 2:
                    day_month = f"{parts[0]}/{parts[1]}"
                    for word in context_lower.split():
                        if day_month in word:
                            date_in_context = True
                            break
            
            hallucination_events.append({
                "date_mentioned": raw,
                "normalized": norm,
                "context_found": date_in_context
            })
    
    return response_text, hallucination_events
