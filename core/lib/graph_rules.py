import difflib
import os
from supabase import create_client, Client

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

BANNED_RELATIONSHIPS = {
    'RELATES_TO', 'BELONGS_TO', 'AUTHORED',
    'FEELS', 'INVOLVES', 'WORKS_WITH', 'KNOWS'
}

INVALID_COMBOS = {
    ("person", "KNOWS", "emotional_state"): ("auto_reject", "emotions on memory metadata"),
    ("person", "BELONGS_TO", "project"): ("auto_correct", "WORKS_AT"),
    ("project", "KNOWS", "person"): ("auto_reject", "projects don't know people"),
    ("task", "BELONGS_TO", "project"): ("auto_correct", "WORKS_ON"),
    ("task", "INVOLVES", "person"): ("auto_correct", "DISCUSSED_WITH"),
    ("organization", "KNOWS", "person"): ("auto_reject", "organizations don't know people"),
    ("person", "OWNS", "person"): ("auto_reject", "OWNS is programmatic-only"),
    ("person", "OWNS", "organization"): ("auto_reject", "OWNS is programmatic-only"),
    ("project", "OWNS", "person"): ("auto_reject", "OWNS is programmatic-only"),
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
    if rel_upper in BANNED_RELATIONSHIPS:
        return {"action": "auto_reject", "reason": f"Banned relationship type: {rel_upper}"}
    key = (source_type, rel_upper, target_type)
    if key in INVALID_COMBOS:
        action, reason = INVALID_COMBOS[key]
        return {"action": action, "reason": reason}
    return {"action": "pass"}
