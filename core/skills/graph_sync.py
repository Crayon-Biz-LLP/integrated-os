import os
import json
from supabase import create_client, Client

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

GRAPH_STORE_PATH = os.path.join(os.path.dirname(__file__), "knowledge", "graph_store")
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
            "node_id": node.get("id"),
            "label": node.get("label", ""),
            "node_type": node.get("type", ""),
            "file_path": node.get("file_path", ""),
            "source_location": node.get("source_location", ""),
            "metadata": json.dumps(node.get("metadata", {}))
        })

    supabase.table("graph_nodes").upsert(
        node_records,
        on_conflict="node_id"
    ).execute()

    return len(node_records)


def sync_edges(graph: dict) -> int:
    edges = graph.get("edges", [])
    if not edges:
        return 0

    edge_records = []
    for edge in edges:
        edge_records.append({
            "source_id": edge.get("source"),
            "target_id": edge.get("target"),
            "relationship": edge.get("relationship", ""),
            "confidence": edge.get("confidence_score", 1.0),
            "source_type": edge.get("source_type", "EXTRACTED")
        })

    supabase.table("graph_edges").upsert(
        edge_records,
        on_conflict="source_id_target_id"
    ).execute()

    return len(edge_records)


def vault_snapshot(graph: dict) -> str:
    snapshot = {
        "graph_data": graph,
        "synced_at": datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
    }

    response = supabase.table("graph_vault").insert({
        "snapshot": json.dumps(snapshot),
        "created_at": snapshot["synced_at"]
    }).execute()

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
        print(f"Vault snapshot stored at {synced_at}")

        print("Graph sync completed successfully.")

    except FileNotFoundError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Graph sync failed: {e}")


if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    run_graph_sync()