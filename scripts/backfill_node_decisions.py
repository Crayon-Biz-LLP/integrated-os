"""Backfill decisions + observations for batch-created nodes (source_text='batch')."""
import asyncio
import sys
sys.path.insert(0, '.')
from core.services.db import get_supabase
from core.lib.telemetry import _update_pattern_count

BATCH = 100

async def run():
    supabase = get_supabase()
    nodes = (supabase.table('pending_nodes').select('id,label,type,source_text,status')
             .eq('source_text', 'batch').in_('status', ['approved', 'auto_approved']).execute()).data or []
    print(f'Batch-created nodes: {len(nodes)}', flush=True)
    if not nodes:
        print('Nothing to do.', flush=True)
        return

    for i in range(0, len(nodes), BATCH):
        batch = nodes[i:i + BATCH]
        supabase.table('decisions').insert([{
            'decision_type': 'graph_node_approval',
            'title': f"Approved {n['type']}: {n['label']}",
            'context': f"Pending node #{n['id']} approved via batch backfill.",
            'entity_type': 'graph_node',
            'entity_id': str(n['id']),
            'confidence': 1.0,
            'source': 'batch_backfill',
            'auto_decided': True,
            'status': 'active',
        } for n in batch]).execute()

        supabase.table('subsystem_telemetry').insert([{
            'subsystem': 'entity_extraction',
            'event_type': 'approval',
            'features': {'node_type': n['type'], 'has_context': bool(n.get('source_text')), 'source': 'batch_backfill'},
            'outcome': 'confirmed',
            'source': 'batch_backfill',
        } for n in batch]).execute()

        for n in batch:
            await _update_pattern_count('entity_extraction', {'node_type': n['type'], 'has_context': bool(n.get('source_text')), 'source': 'batch_backfill'}, 'confirmed')

        print(f'  Batch {i//BATCH + 1}: +{len(batch)}', flush=True)

    print(f'\nDone. {len(nodes)} nodes backfilled.', flush=True)

if __name__ == '__main__':
    asyncio.run(run())
