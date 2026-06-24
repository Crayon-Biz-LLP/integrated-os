import re
import traceback
from core.lib.audit_logger import audit_log_sync


def format_error(e: Exception) -> str:
    """Format exception for logging."""
    return traceback.format_exc() if hasattr(e, '__traceback__') else str(e)

def get_project_name(project: dict) -> str:
    """Normalize project object — handles both DB rows (name) and graph_nodes rows (label)."""
    if not isinstance(project, dict):
        return ""
    return (project.get("name") or project.get("label") or "").strip()

def build_routing_context(legacy_projects: list, organizations: list = None) -> str:
    """
    Dynamically builds project routing instructions from the DB.
    """
    from core.features import is_org_routing_enabled
    if is_org_routing_enabled() and organizations is not None:
        lines = []
        org_id_to_name = {o['id']: o['name'] for o in organizations}
        org_id_to_parent = {o['id']: o.get('parent_organization_id') for o in organizations}
        
        # Build org hierarchy strings
        def get_org_path(oid):
            path = []
            curr = oid
            while curr:
                name = org_id_to_name.get(curr)
                if name:
                    path.insert(0, name)
                curr = org_id_to_parent.get(curr)
                if len(path) > 5:
                    break # guard against cycles
            return " -> ".join(path)
            
        # Group projects by org
        org_projects = {}
        for p in legacy_projects:
            if p.get('status') != 'active':
                continue
            oid = p.get('organization_id')
            if oid not in org_projects:
                org_projects[oid] = []
            org_projects[oid].append(p)
            
        for org in organizations:
            if not org.get('is_active'):
                continue
            org_name = org.get('name', '').strip()
            if not org_name:
                continue
            
            path = get_org_path(org['id'])
            lines.append(f"ORGANIZATION: {path} (Type: {org.get('org_type', 'unknown')})")
            
            projs = org_projects.get(org['id'], [])
            if not projs:
                lines.append(f"  - No specific projects. Use organization_name '{org_name}' directly for general tasks.")
            else:
                for p in sorted(projs, key=lambda x: x.get('name', '')):
                    pname = p.get('name', '').strip()
                    desc = (p.get('description') or '').strip()
                    detail = f"  - PROJECT: {pname} | {desc}"
                    kws = p.get('keywords') or []
                    if kws:
                        detail += f" | Keywords: {', '.join(kws)}"
                    lines.append(detail)
            lines.append("")
        return '\n'.join(lines).strip()
        
    # Legacy flat routing
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

def normalize_cluster_title(value: str) -> str:
    """Normalize cluster title for comparison: lowercase, strip, collapse punctuation."""
    if not value or not isinstance(value, str):
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized
