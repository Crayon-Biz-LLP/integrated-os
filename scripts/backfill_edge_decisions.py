"""Backfill decisions + observations for ALL approved pending_graph_edges."""
import asyncio
import sys
import time
sys.path.insert(0, '.')
from core.services.db import get_supabase
from core.lib.telemetry import _update_pattern_count

BATCH = 100

async def run():
    supabase = get_supabase()

    # Fetch all approved edges (paginated)
    all_edges = []
    offset, bs = 0, 500
    while True:
        r = supabase.table('pending_graph_edges').select(
            'id,source_label,target_label,relationship,source_type,target_type,source_text'
        ).eq('status', 'approved').range(offset, offset + bs - 1).execute()
        data = r.data or []
        all_edges.extend(data)
        if len(data) < bs:
            break
        offset += bs

    print(f'Total approved edges: {len(all_edges)}', flush=True)
    if not all_edges:
        print('Nothing to do.', flush=True)
        return

    # Process in batches
    for i in range(0, len(all_edges), BATCH):
        batch = all_edges[i:i + BATCH]
        t0 = time.time()

        # Bulk insert decisions
        supabase.table('decisions').insert([{
            'decision_type': 'graph_edge_approval',
            'title': f"Approved edge: {e['source_label']} → {e['relationship']} → {e['target_label']}",
            'context': f"Pending edge #{e['id']} approved." + (f" Source: {(e['source_text'])[:200]}" if e.get('source_text') else ''),
            'entity_type': 'graph_edge',
            'entity_id': str(e['id']),
            'confidence': 1.0,
            'source': 'batch_backfill',
            'auto_decided': True,
            'status': 'active',
        } for e in batch]).execute()

        # Bulk insert observations
        supabase.table('subsystem_telemetry').insert([{
            'subsystem': 'entity_extraction',
            'event_type': 'approval',
            'features': {'relationship': e['relationship'], 'source_type': e.get('source_type'), 'target_type': e.get('target_type')},
            'outcome': 'confirmed',
            'source': 'batch_backfill',
        } for e in batch]).execute()

        # Pattern counts — sequential per edge
        for e in batch:
            features = {'relationship': e['relationship'], 'source_type': e.get('source_type'), 'target_type': e.get('target_type')}
            await _update_pattern_count('entity_extraction', features, 'confirmed')

        elapsed = time.time() - t0
        print(f'  Batch {i//BATCH + 1}/{(len(all_edges)-1)//BATCH + 1}: +{len(batch)} in {elapsed:.1f}s', flush=True)

    print(f'\nDone. {len(all_edges)} edges backfilled.', flush=True)

if __name__ == '__main__':
    asyncio.run(run())
