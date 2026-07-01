import re
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

PEOPLE_TITLES = [
    "pastor ", "dr. ", "dr ", "mr. ", "mr ", "mrs. ", "mrs ",
    "ms. ", "ms ", "rev. ", "rev ", "fr. ", "fr ", "saint ",
]


def normalize_person_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"\(.*?\)", "", name).strip()
    for title in PEOPLE_TITLES:
        if name.startswith(title):
            name = name[len(title):]
            break
    return name.strip()


def is_blocklisted_person(name: str) -> bool:
    """Dynamic check — no hardcoded blocklist.
    Only blocks names too short to be real people.
    Everything else passes through to the existing guards:
    - resolve_canonical_label() checks pending_graph_nodes rejected entries
    - Entity extraction has text-anchoring validation
    - HITL requires approval for new person nodes
    - Sync functions skip orphaned [DELETED] entries"""
    if not name:
        return True
    return len(normalize_person_name(name)) < 2


def enrich_people_from_graph() -> int:
    """Enrich people table from graph edges — updates org and last_interaction_date.
    Returns count of people enriched."""
    supabase = get_supabase()
    enriched = 0
    try:
        people_res = supabase.table('people').select('id, name').execute()
        if not people_res.data:
            return 0

        # Get all person graph nodes
        nodes_res = supabase.table('graph_nodes').select('id, label, metadata').eq('type', 'person').execute()
        if not nodes_res.data:
            return 0

        node_to_people = {}
        for node in nodes_res.data:
            meta = node.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    import json
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            people_id = meta.get('people_id')
            if people_id:
                try:
                    node_to_people[node['id']] = int(people_id)
                except (ValueError, TypeError):
                    pass

        # Get all edges involving person nodes
        person_node_ids = list(node_to_people.keys())
        if not person_node_ids:
            return 0

        edges_res = supabase.table('graph_edges').select(
            'source_node_id, target_node_id, relationship, created_at'
        ).or_(
            f'source_node_id.in.({",".join(str(n) for n in person_node_ids)}),'
            f'target_node_id.in.({",".join(str(n) for n in person_node_ids)})'
        ).execute()

        if not edges_res.data:
            return 0

        # Build per-person stats
        from datetime import datetime, timezone
        person_stats = {}  # people_id → {last_edge_at, org_label}
        for edge in edges_res.data:
            src = edge.get('source_node_id')
            tgt = edge.get('target_node_id')
            rel = edge.get('relationship', '')
            created = edge.get('created_at')

            person_id = node_to_people.get(src) or node_to_people.get(tgt)
            if not person_id:
                continue

            if person_id not in person_stats:
                person_stats[person_id] = {'last_edge_at': None, 'org_label': None}

            # Track latest edge
            if created and (not person_stats[person_id]['last_edge_at'] or created > person_stats[person_id]['last_edge_at']):
                person_stats[person_id]['last_edge_at'] = created

            # Track MEMBER_OF edges for org
            if rel == 'MEMBER_OF' and not person_stats[person_id]['org_label']:
                # Find the org label
                other_id = tgt if src == node_to_people.get(person_id) else src
                org_node = supabase.table('graph_nodes').select('label').eq('id', other_id).maybe_single().execute()
                if org_node and org_node.data:
                    person_stats[person_id]['org_label'] = org_node.data['label']

        # Update people table
        for pid, stats in person_stats.items():
            update_data = {}
            if stats['last_edge_at']:
                update_data['last_interaction_date'] = stats['last_edge_at']
            if stats['org_label']:
                update_data['organization_name'] = stats['org_label']
            if update_data:
                update_data['enriched_at'] = datetime.now(timezone.utc).isoformat()
                supabase.table('people').update(update_data).eq('id', pid).execute()
                enriched += 1

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"People enrichment failed: {e}")
    return enriched
