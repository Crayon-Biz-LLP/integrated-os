import os
import json
import uuid
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)

GRAPH_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "knowledge", "graph_store")
GRAPH_JSON_PATH = os.path.join(GRAPH_STORE_PATH, "graph.json")


def load_graph() -> dict:
    if not os.path.exists(GRAPH_JSON_PATH):
        raise FileNotFoundError(f"graph.json not found at {GRAPH_JSON_PATH}")
    with open(GRAPH_JSON_PATH, "r") as f:
        return json.load(f)


def sync_nodes(graph: dict) -> int:
    nodes = graph.get("nodes", [])
    if not nodes:
        return 0

    node_records = []
    for node in nodes:
        node_records.append({
            "label": node.get("label", ""),
            "type": node.get("type", ""),
            "metadata": json.dumps({
                "file_path": node.get("file_path", ""),
                "source_location": node.get("source_location", ""),
                **node.get("metadata", {})
            })
        })

    try:
        supabase.table("graph_nodes").upsert(
            node_records,
            on_conflict="label"
        ).execute()
    except Exception as e:
        print(f"Node upsert error: {e}")
        return 0

    return len(node_records)


def sync_edges(graph: dict) -> int:
    edges = graph.get("edges", [])
    if not edges:
        return 0

    nodes_res = supabase.table("graph_nodes").select("id, label").execute()
    label_to_id = {node["label"]: node["id"] for node in nodes_res.data}

    edge_records = []
    for edge in edges:
        src = str(edge.get("source", ""))
        tgt = str(edge.get("target", ""))
        
        source_node_id = label_to_id.get(src)
        target_node_id = label_to_id.get(tgt)
        
        if not source_node_id or not target_node_id:
            print(f"Skipping edge: {src} -> {tgt} (node not found in DB)")
            continue
        
        edge_records.append({
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "relationship": edge.get("relationship", ""),
            "weight": edge.get("confidence_score", 1.0),
            "metadata": json.dumps({
                "source_type": edge.get("source_type", "EXTRACTED")
            })
        })

    inserted = 0
    for edge in edge_records:
        supabase.table("graph_edges").upsert(
            edge,
            on_conflict="source_node_id,relationship,target_node_id"
        ).execute()
        inserted += 1

    return inserted


def vault_snapshot(graph: dict) -> str:
    from datetime import datetime, timezone, timedelta
    snapshot = {
        "graph_data": graph,
        "synced_at": datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
    }

    try:
        response = supabase.table("graph_vault").insert({
            "graph_data": json.dumps(snapshot)
        }).execute()
    except Exception as e:
        print(f"Vault snapshot error: {e}")
        return None

    return snapshot["synced_at"]


def run_graph_sync():
    try:
        print(f"Loading graph from {GRAPH_JSON_PATH}...")
        graph = load_graph()

        node_count = sync_nodes(graph)
        print(f"Synced {node_count} nodes to graph_nodes table.")

        edge_count = sync_edges(graph)
        print(f"Synced {edge_count} edges to graph_edges table.")

        synced_at = vault_snapshot(graph)
        if synced_at is None:
            print("Vault snapshot failed but sync completed.")
        else:
            print(f"Vault snapshot stored at {synced_at}")

        print("Graph sync completed successfully.")

    except FileNotFoundError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Graph sync failed: {e}")


if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    run_graph_sync()