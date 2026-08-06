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
from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync
from core.lib.entity_detector import detect_entities


@dataclass
class EntityResolution:
    """Result of deterministic entity resolution."""
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
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
        write_signal_on_miss: If True, write to org_creation_signals on failure

    Returns:
        EntityResolution with resolved IDs (may be None if no match found).
        Project matching is intentionally skipped — tasks are assigned to orgs directly.
    """
    # Run deterministic detection (supabase client not needed here — no project lookup required)
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

        elif e.type == 'person' and e.db_id:
            result.person_ids.append(e.db_id)
            result.person_names.append(e.label)
            reason_parts.append(f"person: {e.label}")

    # Note: Project matching is intentionally removed.
    # Tasks are assigned to orgs directly, not to projects.
    # Projects caused cross-org name collisions (e.g. "Digital Marketing"
    # under FC Madras matched for a Marutham task).

    # Write miss signal if nothing found
    if not result.organization_id and write_signal_on_miss:
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
    """Write a signal when entity resolution explicitly tried to resolve an org but found nothing.

    Only fires when the planner explicitly specified an org name (planner_org_name is set).
    Generic tasks like "Buy groceries" that naturally have no org context do NOT trigger a signal.
    This prevents org_creation_signals from being spammed with conversational noise.
    """
    if not planner_org_name:
        return  # Silent skip: no explicit org resolution attempt — not a real miss
    try:
        supabase = tenant_aware_client()
        signal_data = {
            "org_name": f"[unresolved_org={planner_org_name}] {planner_proj_name or text[:50]}",
            "source": "entity_linker",
        }

        supabase.table('org_creation_signals').insert(signal_data).execute()
        audit_log_sync("entity_linker", "INFO",
                       f"Written miss signal: org={planner_org_name}, text={text[:80]}")
    except Exception as e:
        audit_log_sync("entity_linker", "WARNING",
                       f"Failed to write miss signal: {e}")
