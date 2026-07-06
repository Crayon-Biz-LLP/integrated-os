import os
import sys
from dotenv import load_dotenv
load_dotenv(".env.local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402

def main():
    supabase = get_supabase()
    
    test_labels = [
        '[TEST] Boundary Reached Task',
        'SIM_TEST Entity Org',
        'SIM_TEST Entity Project',
        'Test',
        'Test Event',
        'Test Rhodey'
    ]
    
    print("Deleting test nodes...")
    
    nodes_res = supabase.table("graph_nodes").select("id, label").in_("label", test_labels).execute()
    nodes = nodes_res.data
    
    if not nodes:
        print("No test nodes found.")
    else:
        for n in nodes:
            print(f"Deleting node: {n['label']} (id={n['id']})")
            
            # Delete pending edges referencing this node (source or target)
            supabase.table("pending_graph_edges").delete().eq("source_node_id", n['id']).execute()
            supabase.table("pending_graph_edges").delete().eq("target_node_id", n['id']).execute()
            
            # The pending edges without node_ids but with source_label or target_label
            supabase.table("pending_graph_edges").delete().eq("source_label", n['label']).execute()
            supabase.table("pending_graph_edges").delete().eq("target_label", n['label']).execute()

            # Delete the node itself (graph_edges will cascade)
            supabase.table("graph_nodes").delete().eq("id", n['id']).execute()
            
    print("Done cleaning test data.")

if __name__ == "__main__":
    main()
