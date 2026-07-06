import os
import sys
from dotenv import load_dotenv
load_dotenv(".env.local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402

def main():
    supabase = get_supabase()
    
    print("Flushing approved pending edges to graph_edges...")
    
    # Get all approved pending edges with valid node IDs
    offset = 0
    batch_size = 1000
    pending = []
    while True:
        res = supabase.table("pending_graph_edges").select("*") \
            .eq("status", "approved") \
            .not_.is_("source_node_id", "null") \
            .not_.is_("target_node_id", "null") \
            .range(offset, offset + batch_size - 1).execute()
        if not res.data:
            break
        pending.extend(res.data)
        if len(res.data) < batch_size:
            break
        offset += batch_size
        
    print(f"Found {len(pending)} approved pending edges with node IDs.")
    
    # We should insert them into graph_edges if they don't exist
    # graph_edges has a unique constraint `unique_edge` on (source_node_id, relationship, target_node_id)
    # So we can just use upsert with on_conflict="source_node_id, relationship, target_node_id"
    # Wait, the supabase-py client `upsert` takes `on_conflict` string.
    
    to_insert = []
    seen = set()
    
    # 1. Load all valid graph_node IDs to filter out deleted nodes
    valid_node_ids = set()
    offset_n = 0
    while True:
        n_res = supabase.table("graph_nodes").select("id").range(offset_n, offset_n + batch_size - 1).execute()
        if not n_res.data:
            break
        valid_node_ids.update(n['id'] for n in n_res.data)
        if len(n_res.data) < batch_size:
            break
        offset_n += batch_size
        
    for pe in pending:
        if pe["source_node_id"] not in valid_node_ids or pe["target_node_id"] not in valid_node_ids:
            continue
            
        key = (pe["source_node_id"], pe["relationship"], pe["target_node_id"])
        if key in seen:
            continue
        seen.add(key)
        
        # Build graph_edge record
        ge = {
            "source_node_id": pe["source_node_id"],
            "relationship": pe["relationship"],
            "target_node_id": pe["target_node_id"],
            "weight": 1.0,
            "source_ref": pe.get("shortcode", None),
            # copy metadata if any
            "metadata": pe.get("metadata", {})
        }
        to_insert.append(ge)
        
    if to_insert:
        print(f"Upserting {len(to_insert)} edges into graph_edges...")
        # Chunk the upsert to avoid large payloads
        chunk_size = 500
        for i in range(0, len(to_insert), chunk_size):
            chunk = to_insert[i:i+chunk_size]
            res = supabase.table("graph_edges").upsert(
                chunk,
                on_conflict="source_node_id, relationship, target_node_id"
            ).execute()
            print(f"  Upserted chunk of {len(res.data)} edges")
            
    print("\nDone flushing approved edges.")

if __name__ == "__main__":
    main()
