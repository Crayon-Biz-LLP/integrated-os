import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

def run_cleanup():
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

    
    # Get all task nodes
    task_nodes = supabase.table("graph_nodes").select("id, label, metadata").eq("metadata->>source", "transitional").execute()
    
    if not task_nodes.data:
        print("No transitional task nodes found.")
        return
        
    deleted_count = 0
    for node in task_nodes.data:
        node_id = node['id']
        label = node['label']
        
        # Check if the task is done/cancelled
        task = supabase.table("tasks").select("status").eq("content", label).maybe_single().execute()
        if task.data and task.data.get("status") in ["done", "cancelled"]:
            # Check if there are any edges beyond WORKS_ON -> deleted project
            edges = supabase.table("graph_edges").select("id").or_(f"source_node_id.eq.{node_id},target_node_id.eq.{node_id}").execute()
            
            if not edges.data:
                supabase.table("graph_nodes").delete().eq("id", node_id).execute()
                print(f"Deleted orphaned task node: {label}")
                deleted_count += 1
                
    print(f"Deleted {deleted_count} orphaned task nodes.")

if __name__ == "__main__":
    run_cleanup()
