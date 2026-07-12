import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402
from core.pulse.graph import process_pending_edge_decision  # noqa: E402

async def main():
    supabase = get_supabase()
    
    print("=== Job 2: Approving structural pending edges ===")
    res = supabase.table('pending_graph_edges').select('id, relationship, source_type, target_type').eq('status', 'pending').execute()
    
    pending_edges = res.data or []
    count = 0
    for pe in pending_edges:
        if pe['relationship'] == 'BELONGS_TO' and pe['source_type'] in ('project', 'task'):
            print(f"Approving pending edge {pe['id']} ({pe['source_type']} -> {pe['target_type']})")
            result = await process_pending_edge_decision(pe['id'], 'approve', auto_decided=True)
            print(f"  Result: {result}")
            count += 1
            
    print(f"Approved {count} structural pending edges.\n")
    
    print("=== Job 3: Creating WORKS_AT pending edge ===")
    marcus_gn_id = "0e7111de-09c0-431c-a4c1-070b94d4aac7"
    ashraya_gn_id = "579dff52-208c-40eb-a457-c006a7bb9b8f"
    
    pe_data = {
        'source_label': 'Marcus Durai',
        'target_label': 'Ashraya Chennai Central',
        'relationship': 'WORKS_AT',
        'source_text': 'Backfill from existing organization_name column',
        'confidence': 1.0,
        'status': 'pending',
        'source_node_id': marcus_gn_id,
        'target_node_id': ashraya_gn_id,
        'source_type': 'person',
        'target_type': 'organization'
    }
    
    exist = supabase.table('pending_graph_edges').select('id').eq('source_node_id', marcus_gn_id).eq('target_node_id', ashraya_gn_id).eq('relationship', 'WORKS_AT').execute()
    if not exist.data:
        ins = supabase.table('pending_graph_edges').insert(pe_data).execute()
        if ins.data:
            print(f"Created pending edge: {ins.data[0]['id']}")
    else:
        print("Pending edge already exists, skipped.")
        
if __name__ == "__main__":
    asyncio.run(main())
