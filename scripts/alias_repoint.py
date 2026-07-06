import os
import sys
from dotenv import load_dotenv
load_dotenv(".env.local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402

def main():
    supabase = get_supabase()
    
    aliases = {
        'Cesar': 'Cesar Villegas',
        'Charles': 'Charles Sambou',
        'Imran': 'Imran Younis',
        'Keyvan': 'Keyvan Keymanesh',
        'Kiara': 'Kiara Butler',
        'Yashwant': 'Danny',
        'I': 'Danny',
        'danielyashwant@gmail.com': 'Danny'
    }
    
    print("Repointing aliases...")
    
    for old_label, new_label in aliases.items():
        # Get target node ID
        node_res = supabase.table("graph_nodes").select("id, label").eq("label", new_label).execute()
        if not node_res.data:
            # Try case insensitive
            node_res = supabase.table("graph_nodes").select("id, label").ilike("label", new_label).execute()
            
        if not node_res.data:
            print(f"ERROR: Could not find target node for {new_label}")
            continue
            
        new_node_id = node_res.data[0]['id']
        actual_new_label = node_res.data[0]['label']
        
        print(f"\nProcessing alias: {old_label} -> {actual_new_label} ({new_node_id})")
        
        # 1. Repoint pending edges where it's the source
        pe_source_res = supabase.table("pending_graph_edges").update({
            "source_label": actual_new_label,
            "source_node_id": new_node_id
        }).eq("source_label", old_label).execute()
        print(f"  Updated {len(pe_source_res.data)} pending edges (source)")
        
        # 2. Repoint pending edges where it's the target
        pe_target_res = supabase.table("pending_graph_edges").update({
            "target_label": actual_new_label,
            "target_node_id": new_node_id
        }).eq("target_label", old_label).execute()
        print(f"  Updated {len(pe_target_res.data)} pending edges (target)")
        
        # 3. If the old label exists as a node, repoint its graph_edges and delete it
        old_node_res = supabase.table("graph_nodes").select("id").eq("label", old_label).execute()
        if old_node_res.data:
            old_node_id = old_node_res.data[0]['id']
            print(f"  Found existing node for '{old_label}' (id={old_node_id}). Repointing graph_edges...")
            
            # Repoint graph_edges source
            ge_source_res = supabase.table("graph_edges").update({
                "source_node_id": new_node_id
            }).eq("source_node_id", old_node_id).execute()
            print(f"    Updated {len(ge_source_res.data)} graph_edges (source)")
            
            # Repoint graph_edges target
            ge_target_res = supabase.table("graph_edges").update({
                "target_node_id": new_node_id
            }).eq("target_node_id", old_node_id).execute()
            print(f"    Updated {len(ge_target_res.data)} graph_edges (target)")
            
            # Delete old node
            print(f"    Deleting old node '{old_label}'...")
            supabase.table("graph_nodes").delete().eq("id", old_node_id).execute()
            
    print("\nDone repointing aliases.")

if __name__ == "__main__":
    main()
