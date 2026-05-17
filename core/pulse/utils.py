import os
import re
import traceback
from core.lib.audit_logger import audit_log_sync


def format_error(e: Exception) -> str:
    """Format exception for logging."""
    import traceback
    return traceback.format_exc() if hasattr(e, '__traceback__') else str(e)

def get_project_name(project: dict) -> str:
    """Normalize project object — handles both DB rows (name) and graph_nodes rows (label)."""
    if not isinstance(project, dict):
        return ""
    return (project.get("name") or project.get("label") or "").strip()

def build_routing_context(legacy_projects: list) -> str:
    """
    Dynamically builds project routing instructions from the DB.
    No hardcoded client names — new projects auto-register on next Pulse run.
    """
    lines = []

    id_to_name = {p['id']: p['name'] for p in legacy_projects}

    sorted_projects = sorted(
        legacy_projects,
        key=lambda p: (0 if p.get('parent_project_id') else 1, p.get('name', ''))
    )

    for p in sorted_projects:
        if p.get('status') not in ('active',):
            continue

        name = p.get('name', '').strip()
        if not name:
            audit_log_sync("pulse", "WARNING", f"⚠️ Project ID {p.get('id')} has no name, skipping routing context entry.")
            continue

        parent_id = p.get('parent_project_id')
        parent_name = id_to_name.get(parent_id) if parent_id else None
        parent_str = f" [child of {parent_name}]" if parent_name else ""

        desc = (p.get('description') or '').strip()
        detail = f"{name}{parent_str} | {desc}"

        keywords = p.get('keywords') or []
        if keywords:
            detail += f" | Keywords: {', '.join(keywords)}"

        lines.append(detail)

    return '\n'.join(f'  - {line}' for line in lines)

def normalize_mission_title(value: str) -> str:
    """Normalize mission title for comparison: lowercase, strip, collapse punctuation."""
    if not value or not isinstance(value, str):
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized
