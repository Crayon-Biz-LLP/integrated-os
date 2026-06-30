import contextvars
import re
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

def begin_action_context():
    _action_results.set([])

def clear_action_context():
    _action_results.set([])

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
