"""Entity Linker — thin wrapper around entity_detector.

Previously used n-gram matching + planner guess validation + miss signals.
Now delegates all entity detection to core.lib.entity_detector.detect_entities()
(deterministic, no LLM).

Architecture:
    caller ──→ entity_linker.resolve_entities() ──→ entity_detector.detect_entities()
                                                      (deterministic, no LLM)
                               ↓
                        returns EntityResolution
"""

from dataclasses import dataclass, field
from typing import Optional, List
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.lib.entity_detector import detect_entities


@dataclass
class EntityResolution:
    """Result of deterministic entity resolution."""
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    person_ids: List[str] = field(default_factory=list)
    person_names: List[str] = field(default_factory=list)
    source: str = "deterministic"
    confidence: float = 0.0
    reason: str = ""


def resolve_entities(
    text: str,
    planner_org_name: str = None,
    planner_proj_name: str = None,
    write_signal_on_miss: bool = True,
) -> EntityResolution:
    """Deterministically resolve entities from text.

    Args:
        text: Raw user message text
        planner_org_name: Ignored (kept for backward compat)
        planner_proj_name: Ignored (kept for backward compat)
        write_signal_on_miss: If True, write to project_creation_signals on failure

    Returns:
        EntityResolution with resolved IDs (may be None if no match found)
    """
    supabase = get_supabase()

    # Run deterministic detection
    entities = detect_entities(text)

    result = EntityResolution(source="deterministic", confidence=1.0)
    reason_parts = []

    for e in entities:
        if e.type == 'organization' and e.db_id:
            if not result.organization_id:
                result.organization_id = e.db_id
                result.organization_name = e.label
                reason_parts.append(f"org: {e.label}")
            elif e.db_id != result.organization_id:
                reason_parts.append("org_ambiguous")
                result.organization_id = None

        elif e.type == 'project' and e.db_id:
            if not result.project_id:
                result.project_id = e.db_id
                result.project_name = e.label
                reason_parts.append(f"proj: {e.label}")

        elif e.type == 'person' and e.db_id:
            result.person_ids.append(e.db_id)
            result.person_names.append(e.label)
            reason_parts.append(f"person: {e.label}")

    # Infer project's org if project found but no org yet
    if result.project_id and not result.organization_id:
        try:
            proj_res = supabase.table('projects') \
                .select('organization_id') \
                .eq('id', int(result.project_id)) \
                .limit(1).execute()
            if proj_res.data and proj_res.data[0].get('organization_id'):
                result.organization_id = proj_res.data[0]['organization_id']
                # Also get org name
                org_res = supabase.table('organizations') \
                    .select('name') \
                    .eq('id', result.organization_id) \
                    .limit(1).execute()
                if org_res.data:
                    result.organization_name = org_res.data[0]['name']
                reason_parts.append("org_inferred_from_proj")
        except Exception:
            pass

    # Write miss signal if nothing found
    if not result.organization_id and not result.project_id and write_signal_on_miss:
        _write_miss_signal(text, planner_org_name, planner_proj_name)
        result.source = "miss"
        result.confidence = 0.0

    result.reason = " | ".join(reason_parts) if reason_parts else "no_matches"
    return result


def _write_miss_signal(
    text: str,
    planner_org_name: str = None,
    planner_proj_name: str = None,
) -> None:
    """Write a project_creation_signal when entity resolution misses entirely."""
    try:
        supabase = get_supabase()
        signal_data = {
            "project_name": f"[unresolved] {planner_proj_name or text[:50]}",
            "source": "entity_linker",
        }
        if planner_org_name:
            signal_data["project_name"] = \
                f"[unresolved_org={planner_org_name}] {planner_proj_name or text[:50]}"

        supabase.table('project_creation_signals').insert(signal_data).execute()
        audit_log_sync("entity_linker", "INFO",
                       f"Written miss signal: org={planner_org_name}, text={text[:80]}")
    except Exception as e:
        audit_log_sync("entity_linker", "WARNING",
                       f"Failed to write miss signal: {e}")
