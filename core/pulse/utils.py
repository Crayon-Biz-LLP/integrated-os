import re
import traceback


def format_error(e: Exception) -> str:
    """Format exception for logging."""
    return traceback.format_exc() if hasattr(e, '__traceback__') else str(e)

def build_routing_context(organizations: list = None) -> str:
    """
    Dynamically builds org routing instructions from the DB.
    """
    from core.features import is_org_routing_enabled
    if is_org_routing_enabled() and organizations is not None:
        lines = []
        org_id_to_name = {o['id']: o['name'] for o in organizations}
        org_id_to_parent = {o['id']: o.get('parent_organization_id') for o in organizations}
        
        def get_org_path(oid):
            path = []
            curr = oid
            while curr:
                name = org_id_to_name.get(curr)
                if name:
                    path.insert(0, name)
                curr = org_id_to_parent.get(curr)
                if len(path) > 5:
                    break
            return " -> ".join(path)
            
        for org in organizations:
            if not org.get('is_active'):
                continue
            org_name = org.get('name', '').strip()
            if not org_name:
                continue
            path = get_org_path(org['id'])
            lines.append(f"✓ {path} (Type: {org.get('org_type', 'unknown')})")
            lines.append("")
        return '\n'.join(lines).strip()
    
    return ""

def normalize_cluster_title(value: str) -> str:
    """Normalize cluster title for comparison: lowercase, strip, collapse punctuation."""
    if not value or not isinstance(value, str):
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized
