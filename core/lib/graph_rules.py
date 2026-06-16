from core.services.db import get_supabase
import difflib
from dotenv import load_dotenv

load_dotenv()

supabase = get_supabase()

GROUNDED_TYPES = {
    'person':       ('people',        'name'),
    'project':      ('projects',      'name'),
    'organization': ('organizations', 'name'),
}

VALID_EDGE_MATRIX = {
    ('person',       'organization'): ['WORKS_AT', 'CLIENT_OF', 'VENDOR_TO', 'MEMBER_OF', 'SERVES_AT'],
    ('person',       'project'):      ['WORKS_ON', 'LEADS'],
    ('person',       'person'):       ['MET_WITH', 'SPOUSE_OF', 'FAMILY_OF', 'FRIEND_OF', 'KNOWS', 'DISCUSSED_WITH', 'MENTORS'],
    ('person',       'event'):        ['ATTENDED', 'INVOLVES'],
    ('task',         'project'):      ['BELONGS_TO'],
    ('task',         'task'):         ['BLOCKS', 'DEPENDS_ON'],
    ('event',        'project'):      ['PART_OF'],
    ('event',        'person'):       ['INVOLVES'],
    ('project',      'project'):      ['DEPENDS_ON'],
    ('memory',       'person'):       ['MENTIONS'],
    ('memory',       'project'):      ['MENTIONS'],
    ('memory',       'organization'): ['MENTIONS'],
    ('memory',       'event'):        ['MENTIONS'],
    
    # Conceptual fluidity
    ('project',      'concept'):      ['EVOKES', 'RELATES_TO'],
    ('memory',       'concept'):      ['EVOKES'],
    ('event',        'concept'):      ['EVOKES'],
    ('person',       'concept'):      ['ASSOCIATED_WITH'],
    ('task',         'concept'):      ['RELATES_TO'],
    ('organization', 'concept'):      ['ASSOCIATED_WITH'],
}

_alias_cache = None

def resolve_alias(label: str) -> str:
    """Check if the label matches a known alias, and return the canonical name. 
    Otherwise return the original label."""
    global _alias_cache
    if _alias_cache is None:
        try:
            res = supabase.table("person_aliases").select("canonical_name, alias").execute()
            _alias_cache = {r["alias"].lower().strip(): r["canonical_name"] for r in (res.data or [])}
        except Exception:
            _alias_cache = {}
            
    lookup = label.lower().strip()
    if lookup in _alias_cache:
        canonical = _alias_cache[lookup]
        # Write-back async or fire-and-forget
        try:
            # We can't do async easily here, so just sync execute
            res = supabase.table("person_aliases").select("resolution_count").eq("alias", lookup).maybe_single().execute()
            count = res.data.get("resolution_count", 0) if res and res.data else 0
            supabase.table("person_aliases").update({
                "resolution_count": count + 1,
                "last_resolved_at": "now()"
            }).eq("alias", lookup).execute()
        except Exception:
            pass
        return canonical
    return label

NOISE_LABELS = {
    # Pronouns
    'i', 'he', 'she', 'his', 'her', 'they', 'we', 'user', 'the user', 'me', 'my', 'mine',
    # Generic structural terms
    'loops', 'the backlog', 'the author', 'the system', 'the team', 'the person', 'the narrator',
    'nine active projects', 'the board', 'the client', 'the mission', 'the project', 'test', 'docket', 'tasks',
    # Single noise words
    'god', 'app', 'book', 'system', 'project', 'mission', 'church', 'family', 'wife', 'father', 'mother', 'brother', 'sister', 'son', 'daughter', 'husband', 'operations', 'revenue', 'identity', 'prayer', 'revenue'
}

def resolve_canonical_label(raw_label: str) -> dict:
    """Returns the closest canonical match for a raw label.
    
    Resolution chain:
    1. person_aliases table (Amma -> Mother, user -> Danny)
    2. Length check (< 3 chars -> noise)
    3. graph_nodes ILIKE match
    4. pending_graph_nodes ILIKE match
    5. people/projects/organizations ILIKE match
    6. NOISE_LABELS check
    
    Returns: {"label": canonical_label, "node_id": id_or_none, "node_type": type, "exists_in_pending": bool, "confidence": float}
    """
    label = raw_label.strip()
    
    # 1. Alias check
    label = resolve_alias(label)
    
    result = {
        "label": label,
        "node_id": None,
        "node_type": None,
        "exists_in_pending": False,
        "confidence": 0.0
    }
    
    # 2. Length check
    if len(label) < 3:
        return result
        
    # 3. ILIKE match against graph_nodes
    if len(label) >= 4:
        try:
            gn_res = supabase.table("graph_nodes").select("id, label, type").ilike("label", label).maybe_single().execute()
            if gn_res and gn_res.data:
                result["label"] = gn_res.data["label"]
                result["node_id"] = gn_res.data["id"]
                result["node_type"] = gn_res.data["type"]
                result["confidence"] = 1.0
                return result
        except Exception:
            pass
            
        # 4. ILIKE match against pending_graph_nodes
        try:
            pgn_res = supabase.table("pending_graph_nodes").select("id, label, type").ilike("label", label).in_("status", ["pending", "approved", "merge_proposed", "flagged"]).maybe_single().execute()
            if pgn_res and pgn_res.data:
                result["label"] = pgn_res.data["label"]
                result["node_id"] = str(pgn_res.data["id"])
                result["node_type"] = pgn_res.data["type"]
                result["exists_in_pending"] = True
                result["confidence"] = 0.95
                return result
        except Exception:
            pass
            
        # 5. DB lookup for grounded types
        for tbl, typ in [('projects', 'project'), ('people', 'person'), ('organizations', 'organization')]:
            try:
                db_res = supabase.table(tbl).select('id, name').ilike('name', label).maybe_single().execute()
                if db_res and db_res.data:
                    result["label"] = db_res.data["name"]
                    result["node_type"] = typ
                    result["confidence"] = 0.9
                    return result
            except Exception:
                pass
            
    # 6. NOISE_LABELS check
    if label.lower() in NOISE_LABELS:
        result["confidence"] = 0.0
        return result
        
    # Unmatched but passes filters
    return result


def find_similar_node(label: str, node_type: str, threshold: float = 0.55) -> list[dict]:
    result = supabase.table("graph_nodes").select("id, label, type").execute()
    all_nodes = result.data or []
    matches = []
    target_lower = label.lower().strip()
    for n in all_nodes:
        if n.get("type") != node_type:
            continue
        candidate = n.get("label", "")
        ratio = difflib.SequenceMatcher(None, target_lower, candidate.lower().strip()).ratio()
        if ratio >= threshold and target_lower != candidate.lower().strip():
            matches.append({"id": n["id"], "label": candidate, "type": n["type"], "score": round(ratio, 3)})
    return sorted(matches, key=lambda x: -x["score"])


def get_canonical_id(node_id: str) -> str:
    node_res = supabase.table("graph_nodes").select("id, canonical_id").eq("id", node_id).maybe_single().execute()
    if not node_res.data:
        return node_id
    current = node_res.data
    visited = {node_id}
    while current.get("canonical_id"):
        cid = current["canonical_id"]
        if cid in visited:
            return current["id"]
        visited.add(cid)
        next_res = supabase.table("graph_nodes").select("id, canonical_id").eq("id", cid).maybe_single().execute()
        if not next_res.data:
            return current["id"]
        current = next_res.data
    return current["id"]


def propose_merge(source_node_id: str, target_node_id: str) -> dict:
    src_res = supabase.table("graph_nodes").select("label, type").eq("id", source_node_id).maybe_single().execute()
    tgt_res = supabase.table("graph_nodes").select("label").eq("id", target_node_id).maybe_single().execute()
    
    if not src_res or not src_res.data or not tgt_res or not tgt_res.data:
        return {"success": False, "message": "Node not found"}
        
    src_label = src_res.data["label"]
    tgt_label = tgt_res.data["label"]
    
    # Check if already proposed
    existing = supabase.table("pending_graph_nodes")\
        .select("id, status, merge_candidate_id")\
        .ilike("label", src_label)\
        .maybe_single().execute()
        
    if existing and existing.data:
        if existing.data.get("status") == "merge_proposed" and existing.data.get("merge_candidate_id") == target_node_id:
            return {"success": False, "message": "Already proposed"}
        # Update existing record
        supabase.table("pending_graph_nodes").update({
            "status": "merge_proposed",
            "merge_candidate_id": target_node_id,
            "source_text": "dedup_scan"
        }).eq("id", existing.data["id"]).execute()
    else:
        # Insert new merge proposal
        supabase.table("pending_graph_nodes").insert({
            "label": src_label,
            "type": src_res.data["type"],
            "status": "merge_proposed",
            "merge_candidate_id": target_node_id,
            "source_text": "dedup_scan"
        }).execute()
        
    return {"success": True, "message": f"Merge proposed: {src_label} → {tgt_label}"}


def validate_edge(source_type: str, relationship: str, target_type: str) -> dict:
    rel_upper = relationship.upper()
    allowed = VALID_EDGE_MATRIX.get((source_type, target_type), [])
    if rel_upper in allowed:
        return {"action": "pass"}
    return {"action": "auto_reject", "reason": f"Invalid relationship {rel_upper} for {source_type} -> {target_type}"}

def has_structural_anchor(label: str, node_type: str) -> bool:
    if node_type not in GROUNDED_TYPES or GROUNDED_TYPES[node_type] is None:
        return True  # no check available — allow through
    table, column = GROUNDED_TYPES[node_type]
    try:
        result = supabase.table(table).select('id').ilike(column, label.strip()).execute()
        return len(result.data) > 0
    except Exception:
        return True
