import re
from core.services.db import maybe_single_safe, tenant_aware_client
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
    - resolve_canonical_label() checks pending_nodes rejected entries
    - Entity extraction has text-anchoring validation
    - HITL requires approval for new person nodes
    - Sync functions skip orphaned [DELETED] entries"""
    if not name:
        return True
    return len(normalize_person_name(name)) < 2


def enrich_people_from_graph() -> int:
    """Enrich person GRAPH NODES from graph edges — updates
    metadata.enrichment.organization_name and last_interaction_date.

    Consolidation (migration 74): the graph node is the single source of
    truth; the people mirror table is no longer written.
    Returns count of people enriched."""
    supabase = tenant_aware_client()
    enriched = 0
    try:
        # Get all live person graph nodes (the single source of truth)
        nodes_res = supabase.table('graph_nodes').select('id, label, metadata').eq('type', 'person').eq('is_current', True).execute()
        if not nodes_res.data:
            return 0

        person_node_ids = [n['id'] for n in nodes_res.data]

        edges_res = supabase.table('graph_edges').select(
            'source_node_id, target_node_id, relationship, created_at'
        ).or_(
            f'source_node_id.in.({",".join(str(n) for n in person_node_ids)}),'
            f'target_node_id.in.({",".join(str(n) for n in person_node_ids)})'
        ).eq('is_current', True).execute()

        if not edges_res.data:
            return 0

        # Build per-node stats: node_id → {last_edge_at, org_label}
        from datetime import datetime, timezone
        node_stats = {}
        for edge in edges_res.data:
            src = edge.get('source_node_id')
            tgt = edge.get('target_node_id')
            rel = edge.get('relationship', '')
            created = edge.get('created_at')

            node_id = src if src in person_node_ids else (tgt if tgt in person_node_ids else None)
            if not node_id:
                continue

            if node_id not in node_stats:
                node_stats[node_id] = {'last_edge_at': None, 'org_label': None}

            if created and (not node_stats[node_id]['last_edge_at'] or created > node_stats[node_id]['last_edge_at']):
                node_stats[node_id]['last_edge_at'] = created

            if rel == 'MEMBER_OF' and not node_stats[node_id]['org_label']:
                other_id = tgt if src == node_id else src
                org_node = maybe_single_safe(supabase.table('graph_nodes').select('label').eq('id', other_id))
                if org_node and org_node.data:
                    node_stats[node_id]['org_label'] = org_node.data['label']

        # Update graph node metadata.enrichment
        now_iso = datetime.now(timezone.utc).isoformat()
        for node_id, stats in node_stats.items():
            update_data = {}
            if stats['last_edge_at']:
                update_data['last_interaction_date'] = stats['last_edge_at']
            if stats['org_label']:
                update_data['organization_name'] = stats['org_label']
            if not update_data:
                continue
            try:
                node_res = maybe_single_safe(supabase.table('graph_nodes').select('metadata').eq('id', node_id))
                if not node_res or not node_res.data:
                    continue
                node_meta = node_res.data.get('metadata') or {}
                if isinstance(node_meta, str):
                    try:
                        import json
                        node_meta = json.loads(node_meta)
                    except Exception:
                        node_meta = {}
                enrich = dict(node_meta.get('enrichment') or {})
                enrich.update(update_data)
                enrich['enriched_at'] = now_iso
                node_meta['enrichment'] = enrich
                supabase.table('graph_nodes').update({'metadata': node_meta}).eq('id', node_id).execute()
                enriched += 1
            except Exception as write_err:
                audit_log_sync("pulse", "WARNING", f"Enrichment write failed for node {node_id}: {write_err}")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"People enrichment failed: {e}")
    return enriched
