"""Batch approve all pending+rejected graph edges and their missing nodes.

Skips merge_proposed/merged nodes (user decides those manually).

Usage: LIVE_DB=true SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python3 scripts/batch_approve_edges.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()
CUTOFF = "2026-07-04"


def infer_node_type(label: str, edges: list) -> str:
    for e in edges:
        rel = e['relationship']
        if e['source_label'] == label:
            if rel in ('DISCUSSED_WITH', 'MET_WITH', 'WORKS_ON', 'LEADS', 'WORKS_AT', 'KNOWS',
                       'SPOUSE_OF', 'FAMILY_OF', 'FRIEND_OF', 'MENTORS', 'MEMBER_OF',
                       'VENDOR_TO', 'CLIENT_OF', 'SERVES_AT', 'ASSIGNED_TO'):
                return 'person'
        if e['target_label'] == label:
            if rel in ('EVOKES', 'INTERESTED_IN', 'RELATES_TO', 'ASSOCIATED_WITH'):
                return 'concept'
    parts = label.strip().split()
    if len(parts) >= 2 and all(p[0].isupper() for p in parts if p):
        return 'person'
    return 'concept'


def batch(iterable, size=50):
    items = list(iterable)
    return (items[i:i+size] for i in range(0, len(items), size))


def load_gn_map():
    m = {}
    offset = 0
    batch_size = 1000
    while True:
        batch = supabase.table('graph_nodes').select('id, label') \
            .range(offset, offset + batch_size - 1).execute()
        if not batch.data:
            break
        for n in batch.data:
            m[n['label'].lower()] = n['id']
        if len(batch.data) < batch_size:
            break
        offset += batch_size
    return m


def main():
    stats = {"nodes_created": 0, "edges_approved": 0, "skipped_merge": 0, "skipped_merged": 0, "merge_auto": 0, "errors": []}

    # 1. Fetch edges
    edges_res = supabase.table('pending_graph_edges') \
        .select('id, source_label, target_label, relationship, status') \
        .in_('status', ['pending', 'rejected']) \
        .gte('created_at', CUTOFF) \
        .execute()
    edges = edges_res.data or []
    rejected_edges = [e for e in edges if e['status'] == 'rejected']
    pending_edges = [e for e in edges if e['status'] == 'pending']
    print(f"Edges: {len(edges)} ({len(rejected_edges)} rejected, {len(pending_edges)} pending)")

    # 2. Build label set
    labels = set()
    for e in edges:
        labels.add(e['source_label'])
        labels.add(e['target_label'])
    print(f"Unique labels: {len(labels)}")

    # 3. Find missing labels
    gn_map = load_gn_map()
    missing = [lbl for lbl in sorted(labels) if lbl.lower() not in gn_map]
    print(f"Missing from graph_nodes: {len(missing)}")

    # 4. Pre-fetch pending_graph_nodes matching missing labels
    missing_lower = set(lbl.lower() for lbl in missing)
    pgn_all = supabase.table('pending_graph_nodes') \
        .select('id, label, type, status') \
        .execute()
    pgn_map = {}
    for p in (pgn_all.data or []):
        key = p['label'].lower()
        if key in missing_lower:
            pgn_map[key] = p

    # 5. Classify each missing label
    to_insert_gn = []
    to_update_pgn_ids = []
    skipped = []

    for lbl in missing:
        key = lbl.lower()
        pgn = pgn_map.get(key)

        if pgn:
            pgn_id = pgn['id']
            status = pgn['status']
            node_type = pgn['type'] or 'concept'

            if status in ('rejected', 'pending', 'flagged'):
                to_update_pgn_ids.append(pgn_id)
                to_insert_gn.append((lbl, node_type, f'batch_approve_{status}'))
                stats['nodes_created'] += 1
            elif status == 'approved':
                to_insert_gn.append((lbl, node_type, 'batch_approve_fix'))
                stats['nodes_created'] += 1
            elif status == 'merge_proposed':
                node_type = pgn['type'] or infer_node_type(lbl, edges)
                if node_type == 'concept':
                    to_update_pgn_ids.append(pgn_id)
                    to_insert_gn.append((lbl, 'concept', 'batch_approve_merge_concept'))
                    stats['nodes_created'] += 1
                    stats['merge_auto'] += 1
                else:
                    skipped.append((lbl, f'merge_proposed_{node_type}'))
                    stats['skipped_merge'] += 1
            elif status == 'merged':
                skipped.append((lbl, 'merged'))
                stats['skipped_merged'] += 1
        else:
            node_type = infer_node_type(lbl, edges)
            to_insert_gn.append((lbl, node_type, 'batch_approve_create'))
            stats['nodes_created'] += 1

    # 6. Execute batch updates for pending_graph_nodes
    for chunk in batch(to_update_pgn_ids, 200):
        try:
            supabase.table('pending_graph_nodes').update({'status': 'approved'}) \
                .in_('id', chunk) \
                .execute()
        except Exception as e:
            stats['errors'].append(f"pgn status update: {e}")

    print(f"Updated {len(to_update_pgn_ids)} pending_graph_nodes to approved")

    # 7. Batch upsert graph_nodes
    inserted_count = 0
    for chunk in batch(to_insert_gn, 50):
        rows = []
        for label, ntype, src in chunk:
            rows.append({
                "label": label,
                "type": ntype,
                "epistemic_status": "asserted",
                "metadata": {"source": src}
            })
        try:
            res = supabase.table('graph_nodes').upsert(rows, on_conflict="label").execute()
            if res.data:
                for n in res.data:
                    gn_map[n['label'].lower()] = n['id']
                inserted_count += len(rows)
        except Exception as e:
            stats['errors'].append(f"graph_nodes batch upsert: {e}")

    print(f"Created/updated {inserted_count} graph_nodes")

    # 8. Log skipped
    for lbl, reason in skipped:
        audit_log_sync("batch_approve", "INFO", f"Skipped {lbl} ({reason}) — user decides")
    if skipped:
        print(f"Skipped: {len(skipped)} ({stats['skipped_merge']} merge_proposed, {stats['skipped_merged']} merged)")

    # 9. Re-sync gn_map to pick up any nodes from skipped/resolved batches
    gn_map = load_gn_map()
    remaining_missing = len([lbl for lbl in labels if lbl.lower() not in gn_map])
    print(f"Remaining missing from graph_nodes: {remaining_missing}")

    # 10. Approve edges
    for chunk in batch(rejected_edges, 200):
        ids = [e['id'] for e in chunk]
        supabase.table('pending_graph_edges').update({'status': 'pending'}) \
            .in_('id', ids) \
            .execute()

    approved_batch = []
    approved_ids = []
    skipped_count = 0

    for e in edges:
        s_id = gn_map.get(e['source_label'].lower())
        t_id = gn_map.get(e['target_label'].lower())
        if not s_id or not t_id:
            skipped_count += 1
            continue
        approved_batch.append({
            "source_node_id": s_id,
            "target_node_id": t_id,
            "relationship": e['relationship'],
            "weight": 1.0,
            "metadata": {"source": "batch_approve", "pending_id": e['id']}
        })
        approved_ids.append(e['id'])

    # Batch upsert into graph_edges
    ge_count = 0
    for chunk in batch(approved_batch, 50):
        try:
            supabase.table('graph_edges').upsert(chunk,
                on_conflict="source_node_id,relationship,target_node_id",
                ignore_duplicates=True).execute()
            ge_count += len(chunk)
        except Exception as e:
            stats['errors'].append(f"graph_edges upsert: {e}")

    # Mark edges as approved in pending_graph_edges
    for chunk in batch(approved_ids, 200):
        try:
            supabase.table('pending_graph_edges').update({'status': 'approved'}) \
                .in_('id', chunk) \
                .execute()
        except Exception as e:
            stats['errors'].append(f"mark approved: {e}")

    stats['edges_approved'] = ge_count

    print(f"\n{'='*50}")
    print("Results")
    print(f"{'='*50}")
    print(f"Edges approved:  {stats['edges_approved']}/{len(edges)}")
    print(f"  Skipped:       {skipped_count} (missing nodes)")
    print(f"Nodes created:   {stats['nodes_created']}")
    print(f"Merge auto:      {stats['merge_auto']} (concept, direct)")
    print(f"Merge skips:     {stats['skipped_merge']} (entity) + {stats['skipped_merged']} (merged)")
    if stats['errors']:
        print(f"Errors ({len(stats['errors'])}):")
        for err in stats['errors'][:15]:
            print(f"  - {err}")

    # Show some sample skipped edges if any
    if skipped_count > 0:
        sample = [e for e in edges if not gn_map.get(e['source_label'].lower()) or not gn_map.get(e['target_label'].lower())][:5]
        print("\nSample skipped edges:")
        for e in sample:
            s_ok = e['source_label'].lower() in gn_map
            t_ok = e['target_label'].lower() in gn_map
            print(f"  {e['id']}: {e['source_label']}→{e['relationship']}→{e['target_label']}  (src_ok={s_ok}, tgt_ok={t_ok})")


if __name__ == '__main__':
    main()
