"""Backfill merge_proposals rows for stuck merge_proposed pending_nodes."""
import sys
sys.path.insert(0, '.')
from core.services.db import get_supabase
from core.lib.graph_rules import find_similar_node

STUCK_IDS = [339, 351, 355, 357, 365, 390, 404, 407, 413, 414, 479, 480, 485, 486, 489, 498, 503]

def main():
    supabase = get_supabase()
    created = skipped = errors = 0
    for pid in STUCK_IDS:
        res = supabase.table('pending_nodes').select('*').eq('id', pid).single().execute()
        if not res.data:
            print(f"  SKIP id={pid}: not found"); skipped += 1; continue
        row = res.data
        label = row.get('label', '')
        node_type = row.get('node_type', row.get('type', 'person'))
        if not label:
            print(f"  SKIP id={pid}: no label"); skipped += 1; continue
        existing = supabase.table('merge_proposals').select('id').eq('origin_table', 'pending_nodes').eq('origin_id', pid).limit(1).execute()
        if existing.data:
            print(f"  SKIP id={pid}: merge_proposals row already exists (id={existing.data[0]['id']})")
            skipped += 1; continue
        candidates = find_similar_node(label, node_type)
        candidate = candidates[0] if candidates else None
        if not candidate or not candidate.get('id'):
            print(f"  SKIP id={pid} ('{label}'): no similar node found")
            skipped += 1; continue
        target_node_id = candidate['id']
        target_label = candidate.get('label', target_node_id)
        supabase.table('merge_proposals').insert({
            'source_label': label, 'source_type': node_type,
            'target_node_id': target_node_id, 'target_label': target_label,
            'status': 'proposed',
            'rationale': f'Backfill: similar {node_type} node found for pending node #{pid}',
            'origin_table': 'pending_nodes', 'origin_id': pid,
        }).execute()
        print(f"  OK   id={pid} ('{label}' -> '{target_label}' [{target_node_id}])")
        created += 1
    print(f"\nDone. Created: {created}, Skipped: {skipped}, Errors: {errors}")

if __name__ == '__main__':
    main()
