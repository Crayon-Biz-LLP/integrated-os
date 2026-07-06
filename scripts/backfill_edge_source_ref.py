import asyncio
from core.services.db import get_supabase

async def main():
    supabase = get_supabase()
    
    # Get all approved pending edges with a valid source_text
    pe_res = supabase.table('pending_graph_edges') \
        .select('id, source_node_id, target_node_id, relationship, source_text') \
        .eq('status', 'approved') \
        .not_.is_('source_text', 'null') \
        .execute()
        
    pending_edges = pe_res.data or []
    print(f"Found {len(pending_edges)} approved pending edges with source_text.")
    
    updates = 0
    for pe in pending_edges:
        if not pe.get('source_node_id') or not pe.get('target_node_id'):
            continue
            
        # Update matching graph_edges
        res = supabase.table('graph_edges') \
            .update({'source_ref': pe['source_text']}) \
            .eq('source_node_id', pe['source_node_id']) \
            .eq('target_node_id', pe['target_node_id']) \
            .eq('relationship', pe['relationship']) \
            .is_('source_ref', 'null') \
            .execute()
            
        if res and res.data:
            updates += len(res.data)
            
    print(f"Successfully updated source_ref on {updates} graph_edges.")

if __name__ == "__main__":
    asyncio.run(main())
