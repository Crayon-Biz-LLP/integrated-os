"""Entity Linker — deterministic entity resolution orchestrator.

Sits between the planner (LLM-based entity guess) and the executor (DB write).
Replaces the planner's probabilistic org/project guess with deterministic
n-gram matching + substring ILIKE fallback.

Called from create_task_direct() and create_note_direct() in tools.py
to ensure every created task/note has the correct organization_id.

Architecture:
    planner (LLM guess) ──→ entity_linker (deterministic) ──→ executor (correct write)
                                   ↓
                            If both miss → project_creation_signals + alert
"""

from dataclasses import dataclass, field
from typing import Optional, List
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


@dataclass
class EntityResolution:
    """Result of deterministic entity resolution."""
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    person_ids: List[str] = field(default_factory=list)
    person_names: List[str] = field(default_factory=list)
    source: str = "deterministic"  # 'deterministic', 'planner', 'fallback', 'miss'
    confidence: float = 0.0
    reason: str = ""


def resolve_entities(
    text: str,
    planner_org_name: str = None,
    planner_proj_name: str = None,
    write_signal_on_miss: bool = True,
) -> EntityResolution:
    """Deterministically resolve entities from text.

    Uses entity_resolver n-gram matching (fast), then substring ILIKE fallback,
    then validates the planner's guess. Returns the best available result.

    Args:
        text: Raw user message text
        planner_org_name: Organization name guessed by the planner's LLM
        planner_proj_name: Project name guessed by the planner's LLM
        write_signal_on_miss: If True, write to project_creation_signals on failure

    Returns:
        EntityResolution with resolved IDs (may be None if no match found)
    """
    supabase = get_supabase()

    # ── Step 1: Run deterministic n-gram + substring resolver ──
    from core.pulse.entity_resolver import resolve_entities_from_text
    det_org_id, det_proj_id, det_reason = resolve_entities_from_text(text)

    # ── Step 2: Validate planner's guess ──
    validated_org_id = det_org_id
    validated_org_name = None
    validation_note = ""

    if det_org_id:
        # Resolver found an org — get its name
        try:
            org_res = supabase.table('organizations').select('name').eq('id', det_org_id).limit(1).execute()
            if org_res.data:
                validated_org_name = org_res.data[0]['name']
        except Exception:
            pass
        validation_note = "deterministic_match"

    elif planner_org_name:
        # Resolver missed, but planner has a guess — validate it via DB
        try:
            org_res = supabase.table('organizations').select('id, name')\
                .ilike('name', planner_org_name)\
                .limit(1).execute()
            if org_res.data:
                validated_org_id = org_res.data[0]['id']
                validated_org_name = org_res.data[0]['name']
                validation_note = "planner_guess_validated"
            else:
                # Try substring ILIKE fallback for planner's guess
                org_res = supabase.table('organizations').select('id, name')\
                    .ilike('name', f'%{planner_org_name}%')\
                    .limit(1).execute()
                if org_res.data:
                    validated_org_id = org_res.data[0]['id']
                    validated_org_name = org_res.data[0]['name']
                    validation_note = "planner_guess_substring"
        except Exception as e:
            audit_log_sync("entity_linker", "WARNING", f"Planner guess validation failed: {e}")

    # ── Step 3: Resolve project ──
    validated_proj_id = det_proj_id
    validated_proj_name = None

    if det_proj_id:
        try:
            proj_res = supabase.table('projects').select('name').eq('id', det_proj_id).limit(1).execute()
            if proj_res.data:
                validated_proj_name = proj_res.data[0]['name']
        except Exception:
            pass
    elif planner_proj_name and not validated_proj_id:
        try:
            proj_res = supabase.table('projects').select('id, name')\
                .ilike('name', planner_proj_name)\
                .eq('is_current', True)\
                .limit(1).execute()
            if proj_res.data:
                validated_proj_id = proj_res.data[0]['id']
                validated_proj_name = proj_res.data[0]['name']
        except Exception:
            pass

    # ── Step 4: Resolve people mentioned in text ──
    person_ids = []
    person_names = []
    try:
        people_res = supabase.table('people').select('id, name').eq('is_current', True).execute()
        text_lower = text.lower()
        for p in (people_res.data or []):
            pname = p.get('name', '').lower()
            if pname and pname in text_lower:
                person_ids.append(str(p['id']))
                person_names.append(p.get('name', ''))
    except Exception as e:
        audit_log_sync("entity_linker", "WARNING", f"Person resolution failed: {e}")

    # ── Step 5: If both miss, write to project_creation_signals ──
    source = "deterministic"
    confidence = 1.0

    if validated_org_id:
        source = "deterministic"
        confidence = 1.0
    elif planner_org_name and validation_note.startswith("planner"):
        source = "planner"
        confidence = 0.85
    else:
        source = "miss"
        confidence = 0.0
        if write_signal_on_miss:
            _write_miss_signal(text, planner_org_name, planner_proj_name)

    reason_parts = [validation_note or det_reason]
    if person_names:
        reason_parts.append(f"people:{','.join(person_names)}")
    reason = " | ".join(reason_parts)

    return EntityResolution(
        organization_id=validated_org_id,
        organization_name=validated_org_name,
        project_id=validated_proj_id,
        project_name=validated_proj_name,
        person_ids=person_ids,
        person_names=person_names,
        source=source,
        confidence=confidence,
        reason=reason,
    )


def _write_miss_signal(text: str, planner_org_name: str = None, planner_proj_name: str = None) -> None:
    """Write a project_creation_signal when entity resolution misses entirely.

    The signal is consumed by the sentinel piggyback and surfaced to the user
    via Telegram, so the user knows an org couldn't be resolved.
    """
    try:
        supabase = get_supabase()
        signal_data = {
            "project_name": f"[unresolved] {planner_proj_name or text[:50]}",
            "source": "entity_linker",
        }
        if planner_org_name:
            signal_data["project_name"] = f"[unresolved_org={planner_org_name}] {planner_proj_name or text[:50]}"

        supabase.table('project_creation_signals').insert(signal_data).execute()
        audit_log_sync("entity_linker", "INFO",
                       f"Written miss signal: org={planner_org_name}, text={text[:80]}")
    except Exception as e:
        audit_log_sync("entity_linker", "WARNING", f"Failed to write miss signal: {e}")
