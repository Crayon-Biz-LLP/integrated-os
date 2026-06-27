import re
from typing import Tuple, Optional
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

def _normalize(s: str) -> str:
    if not s:
        return ""
    # Remove punctuation, lowercase, normalize whitespace
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return ' '.join(s.split())

def _get_ngrams(words: list[str], n: int) -> set[str]:
    ngrams = set()
    for i in range(len(words) - n + 1):
        ngrams.add(' '.join(words[i:i+n]))
    return ngrams

def resolve_entities_from_text(text: str) -> Tuple[Optional[str], Optional[int], str]:
    """
    Deterministic candidate resolver for orgs and projects.
    Uses exact/alias matching on normalized n-grams.
    Returns (organization_id, project_id, reason_log_string)
    """
    supabase = get_supabase()
    
    # 1. Fetch known orgs and projects
    try:
        orgs_res = supabase.table('organizations').select('id, name').execute()
        projs_res = supabase.table('projects').select('id, name, organization_id').eq('status', 'active').execute()
    except Exception as e:
        audit_log_sync("entity_resolver", "ERROR", f"Failed to fetch entities: {e}")
        return None, None, "db_error"
        
    orgs = orgs_res.data or []
    projs = projs_res.data or []
    
    # 2. Extract normalized n-grams from text (up to 4-grams)
    norm_text = _normalize(text)
    words = norm_text.split()
    text_ngrams = set()
    for i in range(1, 5):
        text_ngrams.update(_get_ngrams(words, i))
        
    # 3. Match orgs
    matched_orgs = []
    for org in orgs:
        norm_name = _normalize(org['name'])
        if norm_name in text_ngrams:
            matched_orgs.append(org)
            
    # 4. Match projects
    matched_projs = []
    for proj in projs:
        norm_name = _normalize(proj['name'])
        if norm_name in text_ngrams:
            matched_projs.append(proj)
            
    # 5. Apply confidence rules
    final_org_id = None
    final_proj_id = None
    reason_parts = []
    
    # Project resolution
    if len(matched_projs) == 1:
        final_proj_id = matched_projs[0]['id']
        reason_parts.append(f"proj_exact_match({matched_projs[0]['name']})")
    elif len(matched_projs) > 1:
        reason_parts.append(f"proj_ambiguous({len(matched_projs)}_matches)")
        
    # Org resolution
    if len(matched_orgs) == 1:
        final_org_id = matched_orgs[0]['id']
        reason_parts.append(f"org_exact_match({matched_orgs[0]['name']})")
    elif len(matched_orgs) > 1:
        reason_parts.append(f"org_ambiguous({len(matched_orgs)}_matches)")
        
    # Inference and Collision handling
    if final_proj_id:
        proj_org_id = matched_projs[0].get('organization_id')
        if proj_org_id:
            if not final_org_id:
                # final_org_id is None either because 0 matches or >1 matches
                if len(matched_orgs) > 1:
                    # Text had multiple orgs. Is the project's org one of them?
                    matched_org_ids = [o['id'] for o in matched_orgs]
                    if proj_org_id in matched_org_ids:
                        final_org_id = proj_org_id
                        reason_parts.append("org_inferred_from_proj_resolved_ambiguity")
                    else:
                        # Collision: project's org is NOT in the matched orgs
                        reason_parts.append("org_proj_collision_leaving_org_null")
                else:
                    # 0 matched orgs in text. Safe to infer.
                    final_org_id = proj_org_id
                    reason_parts.append("org_inferred_from_proj")
            else:
                # final_org_id is already set (exactly 1 matched org)
                if proj_org_id != final_org_id:
                    reason_parts.append("org_proj_collision_leaving_org_null")
                    final_org_id = None
            
    reason = " | ".join(reason_parts) if reason_parts else "no_matches"
    
    # Log if there was ambiguity or collision
    if "ambiguous" in reason or "collision" in reason:
        audit_log_sync("entity_resolver", "INFO", f"Resolver weak/conflict match: {reason}")
        
    return final_org_id, final_proj_id, reason
