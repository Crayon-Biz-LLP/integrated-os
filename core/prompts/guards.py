from typing import Literal

from core.services.user_settings import resolve_user_name


def _user() -> str:
    """Per-tenant display name (settings → env → Danny-era default)."""
    try:
        return resolve_user_name()
    except Exception:
        return "the user"


def get_action_integrity_guard() -> str:
    return """ACTION INTEGRITY: You are a READ-ONLY query engine. You answer questions about existing data. You NEVER create, modify, or delete database records. If your answer describes an action being taken (task created, message sent, person notified), that is a hallucination. Your response must be limited to reporting what already exists in the context provided."""

def get_hallucination_prohibition() -> str:
    return """PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', 'I'll watch', or 'I'll handle it'. You cannot contact people or monitor events. Your only job is to confirm the user's task is SECURED in their system."""

def get_base_persona() -> str:
    # Distilled mini-voice for prompts that don't carry the full spec. MUST
    # stay consistent with RHODEY_VOICE in core/prompts/voice.py — never
    # introduce a rival persona here (that's how voices drift).
    user = _user()
    return f"""You are {user}'s Rhodey — pragmatic, direct, and loyal. You speak like a colleague giving a status update, not a coach: your first sentence answers the question, you use contractions, and you never pep-talk, corporate-speak, or psychologize. Be factual and dry; a warmer line is fine in the evening or on personal matters."""

def inject_guards(purpose: Literal["query", "classify", "briefing", "ingest", "enrichment"]) -> str:
    guards = [get_base_persona()]
    if purpose in ("query", "ingest", "briefing"):
        guards.append(get_action_integrity_guard())
    elif purpose in ("classify", "enrichment"):
        guards.append(get_hallucination_prohibition())
    return "\n\n".join(guards)
