from core.services.db import get_supabase, maybe_single_safe
import difflib
import re
from dotenv import load_dotenv
from core.lib.audit_logger import audit_log_sync

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
    ('task',         'organization'): ['BELONGS_TO'],
    ('event',        'project'):      ['PART_OF'],
    ('event',        'person'):       ['INVOLVES'],
    ('project',      'project'):      ['DEPENDS_ON'],
    ('project',      'organization'): ['BELONGS_TO'],
    ('memory',       'person'):       ['MENTIONS'],
    ('memory',       'project'):      ['MENTIONS'],
    ('memory',       'organization'): ['MENTIONS'],
    ('memory',       'event'):        ['MENTIONS'],
    
    # Conceptual fluidity (removed concept rows)
    
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
    ("project", "organization"): {
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
        except Exception as e:
            audit_log_sync("graph_pipeline", "WARNING", f"Alias write-back failed for '{lookup}': {e}")
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

def normalize_label(label: str) -> str:
    """Normalize a label for identity/conflict matching — lowercase + trimmed."""
    return label.strip().lower()

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

def resolve_canonical_label(raw_label: str, node_type: str = None) -> dict:
    """Returns the closest canonical match for a raw label.
    
    Resolution chain:
    1. person_aliases table (Amma -> Mother, user -> Danny)
    2. Length check (< 3 chars -> noise)
    3. graph_nodes ILIKE match
    4. pending_nodes ILIKE match
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
            gn_res = maybe_single_safe(supabase.table("graph_nodes").select("id, label, type, canonical_id").ilike("label", label).eq('is_current', True))
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
            
        # 4. ILIKE match against pending_nodes
        try:
            pgn_res = maybe_single_safe(supabase.table("pending_nodes").select("id, label, type:node_type, status").ilike("label", label))
            if pgn_res and pgn_res.data:
                if pgn_res.data["status"] == "rejected":
                    result["label"] = pgn_res.data["label"]
                    result["is_rejected"] = True
                    result["confidence"] = 0.0
                    return result
                elif pgn_res.data["status"] in ["pending", "approved", "flagged"]:
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
        #         or if deleted_at is set (new canonical approach)
        try:
            db_res = maybe_single_safe(supabase.table('people').select('id, name, role, deleted_at').ilike('name', label).eq('is_current', True))
            if db_res and db_res.data:
                role = str(db_res.data.get('role') or '')
                is_deleted = False
                if any(m in role for m in ["[DELETED]", "[CHANGED TO ORGANIZATION]", "[MERGED INTO"]):
                    is_deleted = True
                if db_res.data.get('deleted_at'):
                    is_deleted = True
                if not is_deleted:
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
            db_res = maybe_single_safe(supabase.table('projects').select('id, name').ilike('name', label).eq('is_current', True))
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
    result = supabase.table("graph_nodes").select("id, label, type").eq('is_current', True).execute()
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
    src_out_res = supabase.table("graph_edges").select("*").eq("source_node_id", source_id).eq('is_current', True).execute()
    src_in_res = supabase.table("graph_edges").select("*").eq("target_node_id", source_id).eq('is_current', True).execute()
    
    tgt_out_res = supabase.table("graph_edges").select("*").eq("source_node_id", target_id).eq('is_current', True).execute()
    tgt_in_res = supabase.table("graph_edges").select("*").eq("target_node_id", target_id).eq('is_current', True).execute()
    
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
    
    # 7. Set canonical_id and mark loser as not current
    # Setting is_current=false hides the merged entity from all downstream
    # queries (briefs, Live tab, graph visualization) — same as the
    # `canonical_id IS NULL` filter the Live tab uses, but also catches
    # any query that doesn't explicitly filter on canonical_id.
    supabase.table("graph_nodes").update({
        "canonical_id": target_id,
        "is_current": False,
        "metadata": src_meta  # Keep original meta on the loser
    }).eq("id", source_id).execute()
    
    # Update target node meta
    supabase.table("graph_nodes").update({"metadata": merged_meta}).eq("id", target_id).execute()
    
    from core.lib.audit_logger import audit_log_sync

    # 8. Clean up the domain table row for the merged-away entity
    # Without this, the entity detector finds the active domain row and
    # recreates the graph node — causing merged entities to reappear.
    src_type = src_node.get('type')
    src_db_id = src_node.get('db_record_id')
    if src_type == 'person' and src_db_id:
        try:
            supabase.table('people').update({
                'deleted_at': 'now()',
                'is_current': False,
                'strategic_weight': 0,
                'graph_node_id': None
            }).eq('id', src_db_id).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to clean up people row on merge: {e}")
    elif src_type == 'organization' and src_db_id:
        try:
            supabase.table('organizations').update({
                'is_active': False,
                'graph_node_id': None
            }).eq('id', src_db_id).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to clean up org row on merge: {e}")
    elif src_type == 'project' and src_db_id:
        try:
            supabase.table('projects').update({
                'is_current': False,
                'status': 'archived'
            }).eq('id', src_db_id).execute()
        except Exception as e:
            audit_log_sync("pulse", "WARNING", f"Failed to clean up project row on merge: {e}")

    audit_log_sync("pulse", "INFO", f"Merged node {src_node['label']} into {tgt_node['label']} ({provenance})")
    
    return {"success": True, "message": f"Merged {src_node['label']} into {tgt_node['label']}"}

def propose_merge(source_node_id: str, target_node_id: str) -> dict:
    src_res = maybe_single_safe(supabase.table("graph_nodes").select("label, type").eq("id", source_node_id))
    tgt_res = maybe_single_safe(supabase.table("graph_nodes").select("label").eq("id", target_node_id))
    
    if not src_res or not src_res.data or not tgt_res or not tgt_res.data:
        return {"success": False, "message": "Node not found"}
        
    src_label = src_res.data["label"]
    tgt_label = tgt_res.data["label"]
    
    # Write to merge_proposals table (replaces old pending_graph_nodes merge_proposed status)
    from core.lib.node_tables import insert_merge_proposal
    insert_merge_proposal(
        source_label=src_label,
        source_type=src_res.data["type"],
        target_node_id=target_node_id,
        target_label=tgt_label,
        source_node_id=source_node_id,
        rationale="dedup_scan",
    )
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

def validate_label(label: str, hints: dict = None) -> dict:
    """
    Pure lexical and domain-assisted validation. No DB calls.
    Returns: {"verdict": "pass" | "flag" | "reject", "reason": str}
    """
    if not label or not isinstance(label, str):
        return {"verdict": "reject", "reason": "empty or invalid type"}
    
    hints = hints or {}
    lower_label = label.lower().strip()
    
    # Lexical rules (Hard rejects)
    if ',' in label:
        return {"verdict": "reject", "reason": "contains comma"}
        
    rel_pattern = r'\b(my|our|his|her|their|wife|husband|father|mother|brother|sister|son|daughter|friend|colleague|boss|the)\b'
    if re.search(rel_pattern, lower_label):
        # Allow if it's a known exact match despite the pattern
        if lower_label not in hints.get("exact_matches", set()):
            return {"verdict": "reject", "reason": "contains relationship/possessive/article word"}
            
    if "'" in label or "’" in label:
        if lower_label not in hints.get("exact_matches", set()):
            return {"verdict": "reject", "reason": "contains possessive"}
            
    if len(label) > 60:
        return {"verdict": "reject", "reason": "extreme length"}
        
    # Domain-assisted suspicion (Flag)
    words = lower_label.split()
    if len(words) > 3:
        if lower_label not in hints.get("exact_matches", set()):
            return {"verdict": "flag", "reason": ">3 words without domain hint support"}
            
    # Check for fused labels (person + org) if hints provided
    known_people = hints.get("people", set())
    known_orgs = hints.get("orgs", set())
    
    if known_people and known_orgs and len(words) >= 2:
        person_found = False
        org_found = False
        for p in known_people:
            if len(p) > 3 and p in lower_label:
                person_found = True
                break
        for o in known_orgs:
            if len(o) > 3 and o in lower_label:
                org_found = True
                break
        
        if person_found and org_found:
            return {"verdict": "flag", "reason": "fused: matches person and org components"}

    return {"verdict": "pass", "reason": ""}

def resolve_candidate(label: str, normalized: str = None) -> dict:
    """DB-backed resolution against known entities."""
    return resolve_canonical_label(label)

def route_label(resolution: dict, validation: dict) -> str:
    """Pure routing policy based on resolution and validation."""
    if validation.get("verdict") == "reject":
        return "discard"

    # If label was previously rejected via pending_nodes, discard it.
    # Without this check, resolve_canonical_label correctly identifies
    # rejected labels (is_rejected=True, confidence=0.0) but routes back
    # to "pending" because confidence < 0.75. This creates an infinite
    # loop of rejection → recreation for noise labels like "Uncle",
    # "The Boys", etc. that are deleted 10+ times.
    if resolution.get("is_rejected"):
        return "discard"
        
    if resolution.get("confidence", 0.0) >= 0.75:
        return "direct"
        
    if validation.get("verdict") == "flag":
        return "pending"
        
    return "pending"

def persist_label(route: str, resolution: dict, source_info: dict) -> str:
    """Executes the DB write for the candidate node based on the route."""
    if route == "discard":
        return None
        
    label = resolution.get("label")
    typ = resolution.get("node_type") or "concept"
    
    if route == "direct":
        if resolution.get("node_id"):
            return resolution["node_id"]
        
        try:
            res = supabase.table("graph_nodes").upsert({
                "label": label,
                "type": typ,
                "normalized_label": normalize_label(label),
                "metadata": source_info
            }, on_conflict="normalized_label, type").execute()
            if res.data:
                return res.data[0]["id"]
        except Exception as e:
            if hasattr(e, "code") and e.code == "23505":
                existing = maybe_single_safe(supabase.table("graph_nodes").select("id").ilike("label", label).eq('is_current', True))
                if existing and existing.data:
                    return existing.data["id"]
            audit_log_sync("graph_pipeline", "ERROR", f"Failed to persist_label direct: {e}")
            return None
    
    if route == "pending":
        # Dual-write: new table + old table for compat
        existing_p = maybe_single_safe(supabase.table("pending_nodes").select("id").ilike("label", label))
        if existing_p and existing_p.data:
            return str(existing_p.data["id"])

        status = "flagged"
        meta = {"source": source_info} if source_info else {}
        if source_info and source_info.get("flag_reason"):
            meta["flag_reason"] = source_info["flag_reason"]

        try:
            from core.lib.node_tables import insert_pending_node
            new_id = insert_pending_node(
                label=label,
                node_type=typ,
                source_text=source_info.get("source_text", "") if source_info else "",
                eval_context=meta if meta else None,
                status=status,
            )
            if new_id:
                return str(new_id)

            # Fallback to pending_nodes directly
            insert_data = {
                "label": label,
                "node_type": typ,
                "source_text": source_info.get("source_text", "") if source_info else "",
                "status": status,
            }
            if meta:
                insert_data["eval_context"] = meta
            res = supabase.table("pending_nodes").insert(insert_data).execute()
            if res.data:
                return str(res.data[0]["id"])
        except Exception as e:
            if hasattr(e, "code") and e.code == "23505":
                existing = maybe_single_safe(supabase.table("pending_nodes").select("id").ilike("label", label))
                if existing and existing.data:
                    return str(existing.data["id"])
            audit_log_sync("graph_pipeline", "ERROR", f"Failed to persist_label pending: {e}")
            return None
            
    return None

def insert_pending_edge(source_label: str, target_label: str, relationship: str, source_info: dict) -> dict:
    """Shared edge insertion function with case-insensitive dedup and validation."""
    s_type = source_info.get("source_type", "concept")
    t_type = source_info.get("target_type", "concept")
    rel = canonicalize_relationship(relationship, s_type, t_type)
    
    # Validation
    s_type = source_info.get("source_type", "concept")
    t_type = source_info.get("target_type", "concept")
    
    if rel == 'OWNS' and source_label != 'Danny':
        audit_log_sync("graph_pipeline", "INFO", f"Auto-rejected {source_label} --[OWNS]--> {target_label}: OWNS is query-only, use BELONGS_TO")
        return {"status": "rejected", "reason": "OWNS is query-only, use BELONGS_TO (target -> source) instead"}

    vr = validate_edge(s_type, rel, t_type)
    if vr["action"] == "auto_reject":
        audit_log_sync("graph_pipeline", "INFO", f"Auto-rejected {source_label} --[{rel}]--> {target_label}: {vr['reason']}")
        return {"status": "rejected", "reason": vr['reason']}
    elif vr["action"] == "auto_correct":
        rel = vr["reason"]
        
    s_lower = source_label.lower().strip()
    t_lower = target_label.lower().strip()
    r_lower = rel.lower().strip()

    # Dedupe against live graph
    try:
        s_res = resolve_candidate(source_label)
        t_res = resolve_candidate(target_label)
        if s_res.get("node_id") and t_res.get("node_id"):
            existing_graph = maybe_single_safe(
                supabase.table("graph_edges").select("id")
                .eq("source_node_id", s_res["node_id"])
                .eq("target_node_id", t_res["node_id"])
                .ilike("relationship", r_lower)
                .eq('is_current', True)
            )
            if existing_graph and existing_graph.data:
                return {"status": "deduped", "reason": "already_in_graph"}
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"Live graph dedup check failed: {e}")
    
    try:
        existing = supabase.table("pending_graph_edges").select("id, source_text").ilike("source_label", s_lower).ilike("target_label", t_lower).ilike("relationship", r_lower).execute()
        if existing.data:
            row = existing.data[0]
            current_sources = [s.strip() for s in (row.get('source_text') or '').split(',') if s.strip()]
            new_source = source_info.get('source_text', '')
            if new_source and new_source not in current_sources:
                current_sources.append(new_source)
                updated_source_text = ", ".join(current_sources)
                supabase.table("pending_graph_edges").update({"source_text": updated_source_text}).eq("id", row['id']).execute()
            return {"status": "deduped", "id": row['id']}
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"Dedup check failed: {e}")
    
    try:
        res = supabase.table("pending_graph_edges").insert({
            "source_label": source_label,
            "target_label": target_label,
            "relationship": rel,
            "status": "pending",
            "source_text": source_info.get("source_text", ""),
            "source_table": source_info.get("source_table", ""),
            "source_type": s_type,
            "target_type": t_type
        }).execute()
        if res.data:
            return {"status": "inserted", "id": res.data[0]['id']}
    except Exception as e:
        if hasattr(e, "code") and e.code == "23505":
            return {"status": "deduped"}
        audit_log_sync("graph_pipeline", "ERROR", f"Insert edge failed: {e}")
        return {"status": "error", "reason": str(e)}
        
    return {"status": "unknown"}

TYPE_TO_DANNY_EDGE = {
    "project": "OWNS",
    "person": "KNOWS",
    "organization": "WORKS_WITH",
    "place": "VISITED",
    "event": "ATTENDED",
    "animal": "OWNS",
    "emotional_state": "FEELS",
    "resource": "USES",
    "cluster": "OWNS",
    "task": "RELATES_TO"
}
