from core.services.db import maybe_single_safe, tenant_aware_client, get_tenant
import difflib
import re
import time
from dotenv import load_dotenv
from core.lib.audit_logger import audit_log_sync

load_dotenv()

supabase = tenant_aware_client()

GROUNDED_TYPES = {
    'person':       ('people',        'name'),
    'organization': ('organizations', 'name'),
}

VALID_EDGE_MATRIX = {
    ('organization', 'organization'): ['INTRODUCED', 'CLIENT_OF', 'PARENT_OF', 'VENDOR_TO'],
    ('person',       'organization'): ['WORKS_AT', 'CLIENT_OF', 'VENDOR_TO', 'MEMBER_OF', 'SERVES_AT'],
    ('person',       'person'):       ['MET_WITH', 'SPOUSE_OF', 'FAMILY_OF', 'FRIEND_OF', 'KNOWS', 'DISCUSSED_WITH', 'MENTORS'],
    ('person',       'event'):        ['ATTENDED', 'INVOLVES'],
    ('task',         'task'):         ['BLOCKS', 'DEPENDS_ON'],
    ('task',         'person'):       ['INVOLVES', 'RELATES_TO', 'ASSIGNED_TO'],
    ('task',         'organization'): ['BELONGS_TO'],
    ('event',        'person'):       ['INVOLVES'],
    ('memory',       'person'):       ['MENTIONS'],
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
    ("person", "person"): {
        "MEETS_WITH": "MET_WITH",
        "DISCUSSES": "DISCUSSED_WITH",
        "TALKS_TO": "DISCUSSED_WITH",
    },
    ("person", "event"): {
        "ATTENDS": "ATTENDED",
    },
}

def canonicalize_relationship(rel: str, source_type: str, target_type: str) -> str:
    """Map relationship variants to canonical forms."""
    if not rel:
        return ""
    rel_upper = rel.upper()
    alias_map = RELATIONSHIP_ALIASES.get((source_type, target_type), {})
    return alias_map.get(rel_upper, rel_upper)

# Module-level caches keyed BY TENANT: these hold tenant data (person
# labels, aliases, the user's own node) and the queries are tenant-scoped,
# so a single global slot would leak tenant A's resolved people into tenant
# B's lookups (the exact class fixed in classify.py/context.py).
_alias_cache: dict[str, dict] = {}


def _meta_aliases(node) -> list:
    """Extract the metadata.aliases array from a graph node (JSONB-safe)."""
    m = node.get("metadata") or {}
    if isinstance(m, str):
        try:
            import json as _json
            m = _json.loads(m)
        except Exception:
            m = {}
    al = m.get("aliases") or []
    if isinstance(al, str):
        al = [al]
    return [str(a).strip() for a in al if str(a).strip()]


def _build_alias_cache() -> dict:
    """Build {alias_lower: canonical_label} from graph_nodes metadata.aliases.

    Migration 76: aliases live ON the node (single source of truth). The old
    person_aliases table is only consulted as a fallback during the transition
    window (code deployed before the SQL runs) — node data always wins.
    """
    cache = {}
    try:
        res = supabase.table("graph_nodes") \
            .select("id, label, type, is_current, metadata") \
            .in_("type", ["person", "organization"]) \
            .eq("is_current", True) \
            .execute()
        for n in (res.data or []):
            label = (n.get("label") or "").strip()
            if not label:
                continue
            cache[label.lower()] = label  # self-mapping (case-insensitive)
            for a in _meta_aliases(n):
                a_low = a.lower()
                if a_low and a_low != label.lower():
                    cache[a_low] = label
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"Alias cache build from nodes failed: {e}")
    # Transition fallback: person_aliases table may still exist pre-migration.
    try:
        res = supabase.table("person_aliases").select("canonical_name, alias").execute()
        for r in (res.data or []):
            a = (r.get("alias") or "").lower().strip()
            c = (r.get("canonical_name") or "").strip()
            if a and c:
                cache.setdefault(a, c)
    except Exception:
        pass  # table gone post-migration — nodes are authoritative
    return cache


def resolve_alias(label: str) -> str:
    """Check if the label matches a known alias and return the canonical name.

    Reads graph_nodes metadata.aliases (migration 76). Otherwise returns the
    original label. Cache is per-tenant (get_tenant key) — never serve one
    tenant's alias map to another."""
    global _alias_cache
    _uid = get_tenant()
    _alias_key = _uid or "__legacy__"
    _cached = _alias_cache.get(_alias_key)
    if _cached is None:
        _alias_cache[_alias_key] = _build_alias_cache()
        _cached = _alias_cache[_alias_key]

    lookup = label.lower().strip()
    if lookup in _cached:
        canonical = _cached[lookup]
        # Fire-and-forget write-back: bump metadata.alias_usage on the node.
        try:
            res = supabase.table("graph_nodes") \
                .select("id, metadata") \
                .ilike("label", canonical) \
                .eq("is_current", True) \
                .limit(1) \
                .execute()
            if res and res.data:
                node = res.data[0]
                m = node.get("metadata") or {}
                if isinstance(m, str):
                    import json as _json
                    try:
                        m = _json.loads(m)
                    except Exception:
                        m = {}
                usage = dict(m.get("alias_usage") or {})
                usage[lookup] = int(usage.get(lookup, 0)) + 1
                supabase.table("graph_nodes").update({
                    "metadata": {**m, "alias_usage": usage}
                }).eq("id", node["id"]).execute()
        except Exception as e:
            audit_log_sync("graph_pipeline", "WARNING", f"Alias usage write-back failed for '{lookup}': {e}")
        return canonical
    return label


# ── Relationship-aware resolution (migration 76) ─────────────────────────────
# Maps natural-language relationship terms to canonical edge types so a query
# like "what are the tasks related to my wife" resolves via the graph edge
# (Danny --SPOUSE_OF--> Sunjula Daniel) instead of a hardcoded name.
RELATIONSHIP_TERMS = {
    "wife": "SPOUSE_OF",
    "husband": "SPOUSE_OF",
    "spouse": "SPOUSE_OF",
    "mother": "PARENT_OF",
    "mom": "PARENT_OF",
    "father": "PARENT_OF",
    "dad": "PARENT_OF",
    "brother": "SIBLING_OF",
    "sister": "SIBLING_OF",
    "son": "CHILD_OF",
    "daughter": "CHILD_OF",
    "friend": "FRIEND_OF",
    "colleague": "WORKS_WITH",
    "co-worker": "WORKS_WITH",
    "coworker": "WORKS_WITH",
    "teammate": "WORKS_WITH",
    "boss": "WORKS_WITH",
}

_user_node_cache: dict[str, tuple] = {}  # tenant-key -> (ts, value)
_USER_CACHE_TTL = 300  # seconds — node/alias edits must be visible without a restart


def _cache_fresh(cached, ttl: int) -> bool:
    return cached is not None and (time.time() - cached[0]) < ttl


def get_user_node() -> dict | None:
    """Return the live 'user' node (the person the app belongs to).

    Detection order (future-user safe): a live person node whose
    metadata.aliases contains 'user'/'me'/'my'/'i', else the node labeled
    'Danny' (tenant #1 legacy). Per-tenant TTL cache — never serve tenant
    A's user node to tenant B.
    """
    global _user_node_cache
    _uid = get_tenant()
    _user_key = _uid or "__legacy__"
    _cached = _user_node_cache.get(_user_key)
    if _cache_fresh(_cached, _USER_CACHE_TTL):
        return _cached[1]
    if _cached is not None:
        _user_node_cache.pop(_user_key, None)  # stale → evict, cap growth
    try:
        res = supabase.table("graph_nodes") \
            .select("id, label, metadata") \
            .eq("type", "person") \
            .eq("is_current", True) \
            .execute()
        result = None
        for n in (res.data or []):
            label = (n.get("label") or "").lower().strip()
            aliases = [a.lower() for a in _meta_aliases(n)]
            if label in ("danny", "user") or any(a in ("user", "me", "my", "i") for a in aliases):
                result = {"id": n["id"], "label": n.get("label")}
                break
        if not result:
            # Tenant's own root label first (settings-driven, per-tenant)
            root_label = resolve_root_label()
            if root_label:
                res_root = supabase.table("graph_nodes") \
                    .select("id, label") \
                    .eq("type", "person") \
                    .ilike("label", root_label) \
                    .eq("is_current", True) \
                    .limit(1) \
                    .execute()
                if res_root and res_root.data:
                    result = {"id": res_root.data[0]["id"], "label": res_root.data[0]["label"]}
        if not result:
            # Legacy fallback: any person node labeled danny/user even without aliases
            res2 = supabase.table("graph_nodes") \
                .select("id, label") \
                .eq("type", "person") \
                .in_("label", ["Danny", "danny", "DANNY", "User", "user"]) \
                .eq("is_current", True) \
                .limit(1) \
                .execute()
            if res2 and res2.data:
                result = {"id": res2.data[0]["id"], "label": res2.data[0]["label"]}
        _user_node_cache[_user_key] = (time.time(), result)
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"get_user_node failed: {e}")
        _user_node_cache[_user_key] = (time.time(), None)
    return _user_node_cache[_user_key][1]


def resolve_relationship_reference(text: str) -> dict | None:
    """Resolve a relationship mention in a query to a person node.

    e.g. "my wife", "my wife's tasks", "our brother" → follows the user node's
    graph edges (SPOUSE_OF / SIBLING_OF / ...) and returns the OTHER person node.

    Returns {"node_id", "label", "relationship", "term"} or None.
    """
    if not text or not isinstance(text, str):
        return None
    low = text.lower()
    user = get_user_node()
    if not user:
        return None

    referenced = False
    # Relationship ownership matters: 'my wife'/'our wife' (and bare "wife's
    # tasks") are user-relative → resolve against the user node. But 'his
    # wife'/'her husband'/'their sister' refer to SOMEONE ELSE's relationship
    # — resolving those against the user node would answer "Marcus and his
    # wife" with the user's own spouse. Three-way discrimination below.
    for term, rel in RELATIONSHIP_TERMS.items():
        if re.search(rf"\b(?:his|her|their)\s+{re.escape(term)}\b", low):
            continue  # third-person possessive — not the user's relationship
        if not re.search(rf"\b(?:my|our)?\s*{re.escape(term)}\b", low):
            continue
        referenced = True
        try:
            res = supabase.table("graph_edges") \
                .select("source_node_id, target_node_id, relationship") \
                .ilike("relationship", rel) \
                .or_(f"source_node_id.eq.{user['id']},target_node_id.eq.{user['id']}") \
                .eq("is_current", True) \
                .limit(20) \
                .execute()
        except Exception as e:
            audit_log_sync("graph_pipeline", "WARNING", f"Relationship edge lookup failed for '{term}': {e}")
            continue
        for e in (res.data or []):
            other_id = e["target_node_id"] if e["source_node_id"] == user["id"] else e["source_node_id"]
            n = maybe_single_safe(supabase.table("graph_nodes")
                                  .select("id, label, type")
                                  .eq("id", other_id)
                                  .eq("is_current", True))
            if n and n.data and n.data.get("type") == "person":
                return {
                    "node_id": n.data["id"],
                    "label": n.data["label"],
                    "relationship": rel,
                    "term": term,
                }
        # Term found but no matching edge — try remaining terms before giving up
    if referenced:
        return None
    return None


_person_index_cache: dict[str, tuple] = {}  # tenant-key -> (ts, value)
_PERSON_INDEX_TTL = 300  # seconds — new people/aliases become resolvable quickly
_COMMON_QUERY_WORDS = {
    "what", "where", "when", "why", "who", "how", "which", "tasks", "related",
    "about", "does", "doing", "show", "give", "list", "tell", "from", "with",
    "this", "that", "there", "their", "these", "those", "task", "update", "status",
}


def _build_person_index() -> list:
    """Cache live person nodes as (label, aliases, id) for text scanning. TTL'd.

    Per-tenant cache (get_tenant key) — person resolution results are tenant
    data and must never cross the tenant boundary.
    """
    global _person_index_cache
    _uid = get_tenant()
    _person_key = _uid or "__legacy__"
    _cached = _person_index_cache.get(_person_key)
    if _cache_fresh(_cached, _PERSON_INDEX_TTL):
        return _cached[1]
    if _cached is not None:
        _person_index_cache.pop(_person_key, None)  # stale → evict, cap growth
    idx = []
    try:
        res = supabase.table("graph_nodes") \
            .select("id, label, metadata") \
            .eq("type", "person") \
            .eq("is_current", True) \
            .execute()
        for n in (res.data or []):
            label = (n.get("label") or "").strip()
            if not label:
                continue
            idx.append({"id": n["id"], "label": label, "aliases": _meta_aliases(n)})
        # Transition fallback: pre-migration the aliases still live in the
        # person_aliases table — merge them so exact alias matches work before
        # db/76 is applied (node data wins after migration).
        try:
            a_res = supabase.table("person_aliases").select("alias, canonical_name").execute()
            for r in (a_res.data or []):
                alias = (r.get("alias") or "").strip()
                canon = (r.get("canonical_name") or "").strip()
                if not alias or not canon:
                    continue
                for n in idx:
                    if n["label"].lower() == canon.lower():
                        if alias.lower() not in [a.lower() for a in n["aliases"]]:
                            n["aliases"].append(alias)
                        break
        except Exception:
            pass  # table gone post-migration
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"_build_person_index failed: {e}")
    _person_index_cache[_person_key] = (time.time(), idx)
    return idx


def find_person_node_for_mention(mention: str) -> dict | None:
    """Resolve a person mention (label or alias) to a live person node.

    Returns {"node_id", "label"} or None. Used at query time so "sunju" and
    "Sunjula" both resolve to the Sunjula Daniel node via metadata.aliases.
    """
    if not mention or not isinstance(mention, str):
        return None
    mention_low = mention.lower().strip()
    if len(mention_low) < 3:
        return None
    idx = _build_person_index()
    # Exact label / alias match first
    for n in idx:
        if n["label"].lower() == mention_low:
            return {"node_id": n["id"], "label": n["label"], "exact": True}
        for a in n["aliases"]:
            if a.lower() == mention_low:
                return {"node_id": n["id"], "label": n["label"], "exact": True}
    # Substring fallback for partial names (e.g. "sunjula" in "Sunjula Daniel")
    for n in idx:
        label = n["label"]
        if len(label) >= 4 and len(mention_low) >= 4 and mention_low in label.lower():
            return {"node_id": n["id"], "label": label, "exact": False}
    return None


def find_person_node_in_text(text: str) -> dict | None:
    """Scan a full query for any known person (label or alias) and resolve it.

    Priority: relationship terms handled separately; here we match exact
    word-boundary label/alias occurrences, then token-substring matches for
    tokens >= 4 chars (excluding common query words). Returns
    {"node_id", "label", "matched", "exact"} or None.
    """
    if not text or not isinstance(text, str):
        return None
    low = text.lower()
    idx = _build_person_index()

    # 1. Exact word-boundary matches of full label / alias
    for n in idx:
        label = n["label"]
        if re.search(rf"\b{re.escape(label.lower())}\b", low):
            return {"node_id": n["id"], "label": label, "matched": label, "exact": True}
        for a in n["aliases"]:
            if len(a) >= 3 and re.search(rf"\b{re.escape(a.lower())}\b", low):
                return {"node_id": n["id"], "label": label, "matched": a, "exact": True}

    # 2. Token-substring: any token (>=4 chars) that is a substring of a label
    #    and not a common query word (e.g. "sunju" inside "Sunjula Daniel").
    #    Returns exact=False so callers can choose to trust it less.
    tokens = [t.strip(" ,.!?;:'\"") for t in re.split(r"\s+", low)]
    tokens = [t for t in tokens if len(t) >= 4 and t not in _COMMON_QUERY_WORDS]
    for t in tokens:
        for n in idx:
            label = n["label"]
            if len(label) >= len(t) and t in label.lower():
                return {"node_id": n["id"], "label": label, "matched": t, "exact": False}
    return None


def resolve_person_in_query(query: str) -> dict | None:
    """Query-time person resolution: relationship first, then name/alias scan.

    Returns {"node_id", "label", "exact"} or None. This is the entry point
    that makes "my wife" (via SPOUSE_OF edge) AND "sunju" (via
    metadata.aliases) both resolve to Sunjula Daniel's node.
    """
    rel = resolve_relationship_reference(query)
    if rel:
        return {"node_id": rel["node_id"], "label": rel["label"], "exact": True}
    return find_person_node_in_text(query)

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

def sanitize_edge_label(label: str) -> str:
    """Strip LLM echo artifacts from an edge endpoint label.

    The relationship prompt feeds detected entities as '- {label} ({type})'
    lines; the model sometimes echoes the whole formatted string back as an
    edge endpoint ("Pup (animal)" instead of "Pup"). This strips the trailing
    ' (type)' suffix, surrounding quotes, and collapses whitespace so the label
    can be matched against the detected-entity set.

    Hardening: the label echo was one of two root causes of the Aug 6
    mislabel batch (backfill_graph memory_id=batch) — "Rahul Male Pup Rescuer
    (person)", "Puppy (animal)" etc. were stored verbatim as concept nodes.
    """
    if not label or not isinstance(label, str):
        return ""
    lbl = label.strip()
    # Remove wrapping quotes if present ("Pup", 'Pup')
    if len(lbl) >= 2 and lbl[0] == lbl[-1] and lbl[0] in ('"', "'", '`'):
        lbl = lbl[1:-1].strip()
    # Strip a trailing ' (type)' suffix — the LLM echo artifact. Restricted to
    # known entity-type keywords so legit parentheticals ("Qhord (India)")
    # are never mangled.
    m = re.match(r'^(.+?)\s+\((person|animal|organization|organisation|org|project|concept|emotional_state|place|event)\)$', lbl, re.IGNORECASE)
    if m:
        lbl = m.group(1).strip()
    # Collapse internal whitespace
    lbl = re.sub(r'\s+', ' ', lbl).strip()
    return lbl

def resolve_edge_label(raw_label: str, detected_nodes: dict) -> str | None:
    """Resolve an edge endpoint label to a detected node's canonical label.

    Sanitizes the raw label (strips '(type)' echoes, quotes, whitespace) and
    matches it case-insensitively against detected_nodes ({label: type}).
    Returns the canonical detected label when found, None when the endpoint
    is NOT a detected entity — callers must drop the edge, never fabricate a
    type for an unknown endpoint (the pre-hardening 'concept' default).
    """
    cleaned = sanitize_edge_label(raw_label)
    if not cleaned:
        return None
    if cleaned in detected_nodes:
        return cleaned
    lower = cleaned.lower()
    for lbl in detected_nodes:
        if lbl.lower() == lower:
            return lbl
    return None


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
        # 5a/5b: Consolidation (migration 74) — resolve directly against live
        #        graph nodes. is_current=True already excludes merged/deleted
        #        entities (merge sets is_current=false + canonical_id), so the
        #        people/org mirror tables are no longer consulted and the
        #        resurrection-via-stale-mirror bug class is eliminated.
        try:
            db_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type').eq('type', 'person').ilike('label', label).eq('is_current', True))
            if db_res and db_res.data:
                result["label"] = db_res.data["label"]
                result["node_id"] = db_res.data["id"]
                result["node_type"] = "person"
                result["confidence"] = 0.9
                return result
        except Exception:
            pass

        try:
            db_res = maybe_single_safe(supabase.table('graph_nodes').select('id, label, type').eq('type', 'organization').ilike('label', label).eq('is_current', True))
            if db_res and db_res.data:
                result["label"] = db_res.data["label"]
                result["node_id"] = db_res.data["id"]
                result["node_type"] = "organization"
                result["confidence"] = 0.9
                return result
        except Exception:
            pass

        # 5c: (removed — project type eliminated; projects mapped as organizations)



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
        candidate_lower = candidate.lower().strip()
        ratio = difflib.SequenceMatcher(None, target_lower, candidate_lower).ratio()
        # Substring priority boost: if one label is fully contained in the other,
        # boost the score so longer strings with perfect prefix don't lose to
        # shorter false positives (e.g. "kiara" in "Kiara Butler" should beat "kumar")
        if target_lower in candidate_lower or candidate_lower in target_lower:
            ratio += 0.3
        if ratio >= threshold and target_lower != candidate_lower:
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

    # 1. Load edges where source or target is involved (paginated — Aug 27)
    def _paginated_edges(col, nid):
        _PAGE = 1000
        rows = []
        off = 0
        while True:
            pg = supabase.table("graph_edges").select("*").eq(col, nid).eq('is_current', True).range(off, off + _PAGE - 1).execute()
            d = pg.data or []
            rows.extend(d)
            if len(d) < _PAGE:
                break
            off += _PAGE
        return rows

    src_out = _paginated_edges("source_node_id", source_id)
    src_in = _paginated_edges("target_node_id", source_id)
    tgt_out = _paginated_edges("source_node_id", target_id)
    tgt_in = _paginated_edges("target_node_id", target_id)
    
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

    # 8. Domain-table cleanup on merge: NOT NEEDED since migration 75.
    # The people/organizations mirror tables no longer exist — the graph node
    # itself (is_current=false + canonical_id) is the single source of truth.
    # is_current=false prevents the entity detector from resurrecting it.

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
    
    # ASSOCIATED_WITH is universally allowed as a generic fallback edge type
    if rel_upper in allowed or rel_upper == 'ASSOCIATED_WITH':
        return {"action": "pass"}
        
    # Instead of auto-rejecting invalid relationships, correct them to the generic type
    return {"action": "auto_correct", "reason": "ASSOCIATED_WITH"}

def has_structural_anchor(label: str, node_type: str) -> bool:
    """Check whether a live graph node of the given type exists for this label.

    Consolidation (migration 74): the graph node is the single source of
    truth — the mirror tables are no longer consulted.
    """
    if node_type not in GROUNDED_TYPES or GROUNDED_TYPES[node_type] is None:
        return True  # no check available — allow through
    try:
        result = supabase.table('graph_nodes').select('id').eq('type', node_type).ilike('label', label.strip()).eq('is_current', True).execute()
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
    typ = resolution.get("node_type")

    # Hardening (Aug 6 root cause): never silently persist a 'concept' node
    # for a label that has no detected type. Before this guard, any caller
    # that reached the write without a type (e.g. an unknown edge endpoint)
    # auto-vivified a concept node. Fail safe: skip the write + audit.
    if not typ:
        audit_log_sync(
            "graph_pipeline", "WARNING",
            f"persist_skipped_no_type: {label!r} has no detected node_type — "
            f"not persisted (route={route})"
        )
        return None

    if route == "direct":
        if resolution.get("node_id"):
            return resolution["node_id"]
        
        try:
            res = supabase.table("graph_nodes").upsert({
                "label": label,
                "type": typ,
                "normalized_label": normalize_label(label),
                "metadata": source_info
            }, on_conflict="owner_id, normalized_label, type").execute()
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
        # Validation gate: reject common words, short labels, and generic terms
        # that the LLM over-extracts from short text ("Garden", "Tech", "What", etc.)
        _PENDING_NOISE_LABELS = {
            'garden', 'tech', 'romans', 'what', 'you', 'info', 'service', 'order',
            'option', 'identity', 'life', 'mrs', 'shopify', 'tamil', 'wayanad',
            'ramnad', 'jesse', 'paulson', 'starlly', 'news', 'media', 'staff',
            'chief', 'user', 'author', 'speaker', 'translator', 'validator',
            'family', 'friends', 'parents', 'uncle', 'aunt', 'father', 'mother',
            'god', 'devil', 'he', 'she', 'it', 'they', 'them', 'him', 'her',
            'his', 'its', 'our', 'your', 'my', 'the', 'a', 'an', 'is', 'am',
            'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
            'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may',
            'might', 'can', 'shall', 'must', 'need', 'dare', 'ought', 'used',
            'option a', 'option b', 'option c', 'suren and rajesh',
            'lanette burrows and edward robinson', 'famitha and nasreen',
            'cobalt and Finch', 'timmy auditors office',
        }
        label_lower = label.strip().lower()
        if (
            len(label_lower) < 3
            or label_lower in _PENDING_NOISE_LABELS
            or label_lower.split()[0] in _PENDING_NOISE_LABELS
        ):
            audit_log_sync(
                "graph_pipeline", "INFO",
                f"persist_rejected_noise: {label!r} rejected by validation gate (route=pending)"
            )
            return None

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

def resolve_root_label() -> str | None:
    """The tenant's root person label — their own name, resolved per-tenant.

    Order (mirrors archive_ingest.resolve_root_label): core_config
    'archive_root_label' (admin override) → user_settings name → None.
    Callers treat None as fail-closed (no root → no root-anchored writes).
    """
    try:
        cfg = maybe_single_safe(supabase.table("core_config").select("content").eq("key", "archive_root_label"))
        if cfg and cfg.data and cfg.data.get("content"):
            return str(cfg.data["content"]).strip() or None
    except Exception:
        pass
    try:
        from core.services.user_settings import resolve_user_name, current_user_id
        name = resolve_user_name(current_user_id())
        if name:
            return name
    except Exception:
        pass
    return None


# Back-compat alias (M9.1 promoted the resolver to a public name so
# core.skills.backfill_graph and others can import it without a private symbol).
_root_person_label = resolve_root_label


def insert_pending_edge(source_label: str, target_label: str, relationship: str, source_info: dict) -> dict:
    """Shared edge insertion function with case-insensitive dedup and validation."""
    s_type = source_info.get("source_type", "concept")
    t_type = source_info.get("target_type", "concept")
    rel = canonicalize_relationship(relationship, s_type, t_type)
    
    # Validation
    s_type = source_info.get("source_type", "concept")
    t_type = source_info.get("target_type", "concept")
    
    if rel == 'OWNS' and source_label != _root_person_label():
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
        existing = supabase.table("pending_graph_edges").select("id, source_text, confidence").ilike("source_label", s_lower).ilike("target_label", t_lower).ilike("relationship", r_lower).execute()
        if existing.data:
            row = existing.data[0]
            current_sources = [s.strip() for s in (row.get('source_text') or '').split(',') if s.strip()]
            new_source = source_info.get('source_text', '')
            update = {}
            if new_source and new_source not in current_sources:
                current_sources.append(new_source)
                update["source_text"] = ", ".join(current_sources)
            # Corroboration (plans/73): a re-mentioned pair gains confidence, so
            # a connection seen across multiple sources rises above the silent
            # gate's expiry threshold while single-mention noise does not.
            existing_conf = row.get('confidence')
            if existing_conf is None:
                bumped = 0.55
            else:
                try:
                    bumped = min(0.95, float(existing_conf) + 0.2)
                except (TypeError, ValueError):
                    bumped = 0.55
            if bumped != existing_conf:
                update["confidence"] = bumped
            if update:
                supabase.table("pending_graph_edges").update(update).eq("id", row['id']).execute()
            return {"status": "deduped", "id": row['id']}
    except Exception as e:
        audit_log_sync("graph_pipeline", "WARNING", f"Dedup check failed: {e}")
    
    try:
        res = supabase.table("pending_graph_edges").insert({
            "source_label": source_label,
            "target_label": target_label,
            "relationship": rel,
            "status": "pending",
            # Single-source LLM-extracted edge — low default confidence; only
            # corroboration (re-mention) or HITL approval raises it.
            "confidence": 0.55,
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

