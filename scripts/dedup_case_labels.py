import os
import sys
from dotenv import load_dotenv
load_dotenv(".env.local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402

def main():
    supabase = get_supabase()
    
    print("Deduplicating case-collided labels...")
    
    # 1. Get all nodes with pagination
    all_nodes = []
    batch_size = 1000
    offset = 0
    
    while True:
        res = supabase.table("graph_nodes").select("id, label, type, created_at, reference_count").range(offset, offset + batch_size - 1).execute()
        if not res.data:
            break
        all_nodes.extend(res.data)
        if len(res.data) < batch_size:
            break
        offset += batch_size
        
    print(f"Loaded {len(all_nodes)} nodes")
    
    # 2. Group by lower(trim(label))
    groups = {}
    for node in all_nodes:
        # Some labels might be null or empty, skip them
        if not node.get('label'):
            continue
        key = node['label'].strip().lower()
        if key not in groups:
            groups[key] = []
        groups[key].append(node)
        
    # 3. Process groups with > 1 node
    for key, nodes in groups.items():
        if len(nodes) > 1:
            # Sort to pick survivor. 
            # Prefer proper case (not all lower), then older creation date, then higher reference count
            # Actually, let's sort by creation date ascending (oldest first).
            # The older one is usually the original proper-case one.
            sorted_nodes = sorted(nodes, key=lambda x: x['created_at'])
            
            survivor = sorted_nodes[0]
            duplicates = sorted_nodes[1:]
            
            # Print info
            print(f"\nGroup: '{key}'")
            print(f"  Survivor: '{survivor['label']}' (id={survivor['id']}, type={survivor['type']})")
            
            for dup in duplicates:
                print(f"  Merging duplicate: '{dup['label']}' (id={dup['id']}, type={dup['type']})")
                
                # Update graph_edges source
                supabase.table("graph_edges").update({
                    "source_node_id": survivor['id']
                }).eq("source_node_id", dup['id']).execute()
                
                # Update graph_edges target
                supabase.table("graph_edges").update({
                    "target_node_id": survivor['id']
                }).eq("target_node_id", dup['id']).execute()
                
                # Update pending_graph_edges source
                supabase.table("pending_graph_edges").update({
                    "source_node_id": survivor['id']
                }).eq("source_node_id", dup['id']).execute()
                
                # Update pending_graph_edges target
                supabase.table("pending_graph_edges").update({
                    "target_node_id": survivor['id']
                }).eq("target_node_id", dup['id']).execute()
                
                # Delete duplicate node
                supabase.table("graph_nodes").delete().eq("id", dup['id']).execute()

    print("\nDone deduplicating labels.")

if __name__ == "__main__":
    main()
