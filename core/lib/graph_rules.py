import difflib
import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

GROUNDED_TYPES = {
    'person':       ('people',        'name'),
    'project':      ('projects',      'name'),
    'organization': ('organizations', 'name'),
}

VALID_EDGE_MATRIX = {
    ('person',       'organization'): ['WORKS_AT', 'CLIENT_OF', 'VENDOR_TO'],
    ('person',       'project'):      ['WORKS_ON', 'LEADS'],
    ('person',       'person'):       ['MET_WITH', 'SPOUSE_OF', 'FAMILY_OF', 'FRIEND_OF'],
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
    return _alias_cache.get(lookup, label)


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
