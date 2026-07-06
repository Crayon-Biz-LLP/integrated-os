from core.services.db import get_supabase, maybe_single_safe
import difflib
import re
from dotenv import load_dotenv

load_dotenv()

supabase = get_supabase()

GROUNDED_TYPES = {
    'person':       ('people',        'name'),
    'project':      ('projects',      'name'),
    'organization': ('organizations', 'name'),
}

VALID_EDGE_MATRIX = {
    ('organization', 'organization'): ['INTRODUCED', 'CLIENT_OF', 'PARENT_OF'],
    ('person',       'organization'): ['WORKS_AT', 'CLIENT_OF', 'VENDOR_TO', 'MEMBER_OF', 'SERVES_AT'],
    ('person',       'project'):      ['WORKS_ON', 'LEADS'],
    ('person',       'person'):       ['MET_WITH', 'SPOUSE_OF', 'FAMILY_OF', 'FRIEND_OF', 'KNOWS', 'DISCUSSED_WITH', 'MENTORS'],
    ('person',       'event'):        ['ATTENDED', 'INVOLVES'],
    ('task',         'project'):      ['BELONGS_TO'],
    ('task',         'task'):         ['BLOCKS', 'DEPENDS_ON'],
    ('task',         'person'):       ['INVOLVES', 'RELATES_TO', 'ASSIGNED_TO'],
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
    
    # New types — place, animal, emotional_state, practice
    ('place',          'person'):      ['RELATES_TO'],
    ('animal',         'person'):      ['RELATES_TO'],
    ('emotional_state','person'):      ['RELATES_TO'],
    ('practice',       'practice'):    ['ASSOCIATED_WITH'],
}

RELATIONSHIP_ALIASES = {
    ("person", "organization"): {
        "WORKS_FOR": "WORKS_AT",
        "EMPLOYED_BY": "WORKS_AT",
        "EMPLOYEE_OF": "MEMBER_OF",
        "EMPLOYEE": "MEMBER_OF",
    },
    ("person", "project"): {
        "LEAD": "LEADS",
        "CONTRIBUTES_TO": "WORKS_ON",
    },
    ("person", "person"): {
        "MEETS_WITH": "MET_WITH",
        "DISCUSSES": "DISCUSSED_WITH",
        "TALKS_TO": "DISCUSSED_WITH",
    },
    ("person", "event"): {
        "ATTENDS": "ATTENDED",
    },
    ("task", "project"): {
        "PART_OF": "BELONGS_TO",
    },
}

def canonicalize_relationship(rel: str, source_type: str, target_type: str) -> str:
    """Map relationship variants to canonical forms."""
    if not rel:
        return ""
    rel_upper = rel.upper()
    alias_map = RELATIONSHIP_ALIASES.get((source_type, target_type), {})
    return alias_map.get(rel_upper, rel_upper)

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
            res = maybe_single_safe(supabase.table("person_aliases").select("resolution_count").eq("alias", lookup))
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
    'i', 'he', 'she', 'his', 'her', 'they', 'we', 'user', 'the user', 'me', 'my', 'mine', 'you', 'your', 'yours', 'him', 'us', 'our', 'ours', 'them', 'their', 'theirs',
    # Generic structural terms
    'loops', 'the backlog', 'the author', 'the system', 'the team', 'the person', 'the narrator',
    'nine active projects', 'the board', 'the client', 'the mission', 'the project', 'test', 'docket', 'tasks',
    # Single noise words
    'god', 'app', 'book', 'system', 'project', 'mission', 'church', 'family', 'wife', 'father', 'mother', 'brother', 'sister', 'son', 'daughter', 'husband', 'operations', 'revenue', 'identity', 'prayer', 'revenue'
}

def normalize_label_comparison(label: str) -> str:
    """Normalize a label for comparison/dedup purposes only.
    Output is NEVER stored — only used for matching.
    
    Transformations:
    - strip whitespace
    - lowercase
    - collapse multiple spaces to single space
    
    Characters STRIPPED: . , ; : ! ? ( ) [ ] { }
    Characters KEPT: a-z 0-9 apostrophe(') hyphen(-) underscore(_) spaces
    """
    if not label:
        return ""
    label = label.strip().lower()
    label = re.sub(r'\s+', ' ', label)
    label = re.sub(r'[.,;:!?()\[\]{}]', '', label)
    return label.strip()

def normalize_label_display(label: str) -> str:
    """Canonical display form for storage.
    
    Transformations:
    - strip whitespace
    - collapse multiple spaces to single space
    
    Characters KEPT: everything (apostrophes, hyphens, original casing)
    NO title-case, NO lowercasing, NO character removal.
    """
    if not label:
        return ""
    label = label.strip()
    label = re.sub(r'\s+', ' ', label)
    return label

def normalize_label(label: str) -> str:
    """Normalize a label for consistent comparison — matches pending_graph_nodes unique index."""
    return label.strip().lower() if label else ""


def resolve_canonical_label(raw_label: str, node_type: str = None) -> dict:
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
    label = normalize_label_display(raw_label)
    
    # 1. Alias check
    label = resolve_alias(label)
    
    result = {
        "label": label,
        "node_id": None,
        "node_type": None,
        "exists_in_pending": False,
        "is_rejected": False,
        "confidence": 0.0
    }
    
    if len(label) < 3:
        return result
        
    # We will use ILIKE against the raw display label for now, since we can't easily 
    # normalize DB labels in a simple select query without a derived column.
    # ILIKE on the display label is safe and catches casing differences.
    if len(label) >= 4:
        try:
            gn_res = maybe_single_safe(supabase.table("graph_nodes").select("id, label, type, canonical_id").ilike("label", label))
            if gn_res and gn_res.data:
                node_id = gn_res.data["id"]
                canonical_id = gn_res.data.get("canonical_id")
                if canonical_id:
                    # Follow canonical chain
                    canonical = get_canonical_id(node_id)
                    canonical_res = maybe_single_safe(supabase.table("graph_nodes").select("id, label, type").eq("id", canonical))
                    if canonical_res and canonical_res.data:
                        result["label"] = canonical_res.data["label"]
                        result["node_id"] = canonical_res.data["id"]
                        result["node_type"] = canonical_res.data["type"]
                        result["confidence"] = 1.0
                        return result
                result["label"] = gn_res.data["label"]
                result["node_id"] = node_id
                result["node_type"] = gn_res.data["type"]
                result["confidence"] = 1.0
                return result
        except Exception:
            pass
            
        # 4. ILIKE match against pending_graph_nodes
        try:
            pgn_res = maybe_single_safe(supabase.table("pending_graph_nodes").select("id, label, type, status").ilike("label", label))
            if pgn_res and pgn_res.data:
                if pgn_res.data["status"] == "rejected":
                    result["label"] = pgn_res.data["label"]
                    result["is_rejected"] = True
                    result["confidence"] = 0.0
                    return result
                elif pgn_res.data["status"] in ["pending", "approved", "merge_proposed", "flagged"]:
                    result["label"] = pgn_res.data["label"]
                    result["node_id"] = str(pgn_res.data["id"])
                    result["node_type"] = pgn_res.data["type"]
                    result["exists_in_pending"] = True
                    result["confidence"] = 0.95
                    return result
        except Exception:
            pass
            
        # 5. DB lookup for grounded types — exact guard pattern (not order-dependent)
        # 5a: People table — skip if role marks deletion/org-change/merge
        try:
            db_res = maybe_single_safe(supabase.table('people').select('id, name, role').ilike('name', label))
            if db_res and db_res.data:
                role = str(db_res.data.get('role') or '')
                if not any(m in role for m in ["[DELETED]", "[CHANGED TO ORGANIZATION]", "[MERGED INTO"]):
                    result["label"] = db_res.data["name"]
                    result["node_type"] = "person"
                    result["confidence"] = 0.9
                    return result
        except Exception:
            pass

        # 5b: Organizations table
        try:
            db_res = maybe_single_safe(supabase.table('organizations').select('id, name').ilike('name', label))
            if db_res and db_res.data:
                result["label"] = db_res.data["name"]
                result["node_type"] = "organization"
                result["confidence"] = 0.9
                return result
        except Exception:
            pass

        # 5c: Projects table
        try:
            db_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', label))
            if db_res and db_res.data:
                result["label"] = db_res.data["name"]
                result["node_type"] = "project"
                result["confidence"] = 0.9
                return result
        except Exception:
            pass

    # 6. NOISE_LABELS check
    if label.lower() in NOISE_LABELS:
        result["confidence"] = 0.0
        return result
        
    # 7. Conservative fuzzy match as last resort
    if node_type and len(label) >= 4:
        fuzzy = find_similar_node(label, node_type, threshold=0.85)
        if fuzzy:
            top = fuzzy[0]
            result["label"] = top["label"]
            result["node_id"] = top["id"]
            result["node_type"] = top["type"]
            result["confidence"] = 0.75  # Needs edge approval but reuses node
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
    node_res = maybe_single_safe(supabase.table("graph_nodes").select("id, canonical_id").eq("id", node_id))
    if not node_res.data:
        return node_id
    current = node_res.data
    visited = {node_id}
    while current.get("canonical_id"):
        cid = current["canonical_id"]
        if cid in visited:
            return current["id"]
        visited.add(cid)
        next_res = maybe_single_safe(supabase.table("graph_nodes").select("id, canonical_id").eq("id", cid))
        if not next_res.data:
            return current["id"]
        current = next_res.data
    return current["id"]


def execute_graph_node_merge(source_id: str, target_id: str, provenance: str = "user_merge") -> dict:
    """
    Merge source graph_node into target graph_node.
    
    Idempotent: if source_node.canonical_id is already set, skip.
    """
    if source_id == target_id:
        return {"success": False, "message": "Source and target are the same node"}

    src_res = maybe_single_safe(supabase.table("graph_nodes").select("*").eq("id", source_id))
    tgt_res = maybe_single_safe(supabase.table("graph_nodes").select("*").eq("id", target_id))
    
    if not src_res or not src_res.data or not tgt_res or not tgt_res.data:
        return {"success": False, "message": "Source or target node not found"}
        
    src_node = src_res.data
    if src_node.get("canonical_id"):
        return {"success": True, "message": "Node already merged"}

    # 1. Load edges where source or target is involved
    src_out_res = supabase.table("graph_edges").select("*").eq("source_node_id", source_id).execute()
    src_in_res = supabase.table("graph_edges").select("*").eq("target_node_id", source_id).execute()
    
    tgt_out_res = supabase.table("graph_edges").select("*").eq("source_node_id", target_id).execute()
    tgt_in_res = supabase.table("graph_edges").select("*").eq("target_node_id", target_id).execute()
    
    src_out = src_out_res.data or []
    src_in = src_in_res.data or []
    tgt_out = tgt_out_res.data or []
    tgt_in = tgt_in_res.data or []
    
    edges_to_delete = []
    edges_to_update_out = []
    edges_to_update_in = []
    
    # 2. Reconcile OUTGOING edges (source -> X vs target -> X)
    tgt_out_map = { f"{e['relationship']}|{e['target_node_id']}": e for e in tgt_out }
    
    for src_edge in src_out:
        key = f"{src_edge['relationship']}|{src_edge['target_node_id']}"
        if key in tgt_out_map:
            tgt_edge = tgt_out_map[key]
            edges_to_delete.append(src_edge['id'])
            
            # Merge metadata into the target edge
            src_meta = src_edge.get("metadata") or {}
            tgt_meta = tgt_edge.get("metadata") or {}
            merged_meta = {**src_meta, **tgt_meta}
            
            all_sources = set()
            if tgt_meta.get("source_text"):
                all_sources.update([s.strip() for s in tgt_meta["source_text"].split(",") if s.strip()])
            if src_meta.get("source_text"):
                all_sources.update([s.strip() for s in src_meta["source_text"].split(",") if s.strip()])
                
            if all_sources:
                merged_meta["source_text"] = ", ".join(all_sources)
                
            supabase.table("graph_edges").update({"metadata": merged_meta}).eq("id", tgt_edge["id"]).execute()
        else:
            edges_to_update_out.append(src_edge['id'])

    # 3. Reconcile INCOMING edges (X -> source vs X -> target)
    tgt_in_map = { f"{e['source_node_id']}|{e['relationship']}": e for e in tgt_in }
    
    for src_edge in src_in:
        key = f"{src_edge['source_node_id']}|{src_edge['relationship']}"
        if key in tgt_in_map:
            tgt_edge = tgt_in_map[key]
            edges_to_delete.append(src_edge['id'])
            
            src_meta = src_edge.get("metadata") or {}
            tgt_meta = tgt_edge.get("metadata") or {}
            merged_meta = {**src_meta, **tgt_meta}
            
            all_sources = set()
            if tgt_meta.get("source_text"):
                all_sources.update([s.strip() for s in tgt_meta["source_text"].split(",") if s.strip()])
            if src_meta.get("source_text"):
                all_sources.update([s.strip() for s in src_meta["source_text"].split(",") if s.strip()])
                
            if all_sources:
                merged_meta["source_text"] = ", ".join(all_sources)
                
            supabase.table("graph_edges").update({"metadata": merged_meta}).eq("id", tgt_edge["id"]).execute()
        else:
            edges_to_update_in.append(src_edge['id'])

    # Handle self-referential loops created by merging
    for eid in edges_to_update_out[:]:
        edge = next(e for e in src_out if e['id'] == eid)
        if edge['target_node_id'] == target_id:
            edges_to_delete.append(eid)
            edges_to_update_out.remove(eid)
            
    for eid in edges_to_update_in[:]:
        edge = next(e for e in src_in if e['id'] == eid)
        if edge['source_node_id'] == target_id:
            if eid not in edges_to_delete:
                edges_to_delete.append(eid)
            if eid in edges_to_update_in:
                edges_to_update_in.remove(eid)

    # 4. Safe Deletions
    if edges_to_delete:
        for i in range(0, len(edges_to_delete), 100):
            batch = edges_to_delete[i:i+100]
            supabase.table("graph_edges").delete().in_("id", batch).execute()

    # 5. Safe Repointing
    if edges_to_update_out:
        for i in range(0, len(edges_to_update_out), 100):
            batch = edges_to_update_out[i:i+100]
            supabase.table("graph_edges").update({"source_node_id": target_id}).in_("id", batch).execute()
            
    if edges_to_update_in:
        for i in range(0, len(edges_to_update_in), 100):
            batch = edges_to_update_in[i:i+100]
            supabase.table("graph_edges").update({"target_node_id": target_id}).in_("id", batch).execute()

    # 6. Merge metadata
    tgt_node = tgt_res.data
    src_meta = src_node.get("metadata") or {}
    tgt_meta = tgt_node.get("metadata") or {}
    merged_meta = {**src_meta, **tgt_meta}
    
    # 7. Set canonical_id
    supabase.table("graph_nodes").update({
        "canonical_id": target_id,
        "metadata": src_meta  # Keep original meta on the loser
    }).eq("id", source_id).execute()
    
    # Update target node meta
    supabase.table("graph_nodes").update({"metadata": merged_meta}).eq("id", target_id).execute()
    
    from core.lib.audit_logger import audit_log_sync
    audit_log_sync("pulse", "INFO", f"Merged node {src_node['label']} into {tgt_node['label']} ({provenance})")
    
    return {"success": True, "message": f"Merged {src_node['label']} into {tgt_node['label']}"}

def propose_merge(source_node_id: str, target_node_id: str) -> dict:
    src_res = maybe_single_safe(supabase.table("graph_nodes").select("label, type").eq("id", source_node_id))
    tgt_res = maybe_single_safe(supabase.table("graph_nodes").select("label").eq("id", target_node_id))
    
    if not src_res or not src_res.data or not tgt_res or not tgt_res.data:
        return {"success": False, "message": "Node not found"}
        
    src_label = src_res.data["label"]
    tgt_label = tgt_res.data["label"]
    
    # Check if already proposed
    existing = maybe_single_safe(
        supabase.table("pending_graph_nodes").select("id, status, merge_candidate_id")
        .ilike("label", src_label)
    )
        
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

def make_memory_preview(content: str, max_words: int = 4) -> str | None:
    """Extract first 2-4 meaningful words from memory content as a short title."""
    import re
    if not content:
        return None
    words = re.findall(r'[A-Za-z]\w+', content)
    meaningful = [w for w in words if len(w) > 2][:max_words]
    return ' '.join(meaningful) if meaningful else None
