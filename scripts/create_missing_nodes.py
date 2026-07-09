import os
import sys
from dotenv import load_dotenv
load_dotenv(".env.local")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402
from core.lib.graph_rules import normalize_label  # noqa: E402

def main():
    supabase = get_supabase()
    
    missing_nodes = [
        {"label": "Amine", "type": "person"},
        {"label": "Architecture walkthrough", "type": "concept"},
        {"label": "Second phase discussion", "type": "concept"},
        {"label": "Governance evidence collector agent", "type": "concept"},
        {"label": "SharePoint", "type": "concept"},
        {"label": "IAM Recertification", "type": "project"},
        {"label": "IAM Recertification weekly cadence session", "type": "event"},
        {"label": "frustrated", "type": "emotional_state"}
    ]
    
    print("Creating missing nodes...")
    
    for node_data in missing_nodes:
        label = node_data["label"]
        node_type = node_data["type"]
        
        # Check if it exists
        existing_res = supabase.table("graph_nodes").select("id").eq("label", label).execute()
        
        if existing_res.data:
            node_id = existing_res.data[0]['id']
            print(f"Node '{label}' already exists (id={node_id})")
        else:
            print(f"Creating '{label}' ({node_type})...")
            # For projects, we should ideally link it to db_record_id, but we might not have it here easily.
            # We'll just create the graph_node for now.
            insert_res = supabase.table("graph_nodes").insert({
                "label": label,
                "type": node_type,
                "normalized_label": normalize_label(label),
            }).execute()
            node_id = insert_res.data[0]['id']
            print(f"  Created id={node_id}")
            
        # Backfill node_ids on pending_edges
        pe_source_res = supabase.table("pending_graph_edges").update({
            "source_node_id": node_id
        }).eq("source_label", label).is_("source_node_id", "null").execute()
        if pe_source_res.data:
            print(f"  Backfilled {len(pe_source_res.data)} pending edges (source)")
            
        pe_target_res = supabase.table("pending_graph_edges").update({
            "target_node_id": node_id
        }).eq("target_label", label).is_("target_node_id", "null").execute()
        if pe_target_res.data:
            print(f"  Backfilled {len(pe_target_res.data)} pending edges (target)")

    print("\nDone creating missing nodes.")

if __name__ == "__main__":
    main()
