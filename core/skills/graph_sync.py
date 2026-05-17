import os
import json

from core.services.db import get_supabase

supabase = get_supabase()

GRAPH_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "knowledge", "graph_store")
GRAPH_JSON_PATH = os.path.join(GRAPH_STORE_PATH, "graph.json")


def load_graph():
    if not os.path.exists(GRAPH_JSON_PATH):
        return {"nodes": [], "edges": []}
    with open(GRAPH_JSON_PATH) as f:
        return json.load(f)


def sync_nodes(graph):
    nodes = graph.get("nodes", [])
    if not nodes:
        print("No nodes to sync.")
        return

    existing = {row["id"] for row in supabase.table("graph_nodes").select("id").execute().data or []}
    to_delete = existing - {n["id"] for n in nodes}
    if to_delete:
        supabase.table("graph_nodes").delete().in_("id", list(to_delete)).execute()
        print(f"Deleted {len(to_delete)} stale nodes.")

    for node in nodes:
        supabase.table("graph_nodes").upsert(node, on_conflict=["id"]).execute()
    print(f"Synced {len(nodes)} graph nodes.")


def sync_edges(graph):
    edges = graph.get("edges", [])
    if not edges:
        supabase.table("graph_edges").delete().neq("id", 0).execute()
        print("Cleared all edges.")
        return

    existing = {row["id"] for row in supabase.table("graph_edges").select("id").execute().data or []}
    to_delete = existing - {e["id"] for e in edges}
    if to_delete:
        supabase.table("graph_edges").delete().in_("id", list(to_delete)).execute()
        print(f"Deleted {len(to_delete)} stale edges.")

    for edge in edges:
        supabase.table("graph_edges").upsert(edge, on_conflict=["id"]).execute()
    print(f"Synced {len(edges)} graph edges.")


def vault_snapshot(graph):
    graph_path = os.path.join(os.path.dirname(__file__), "..", "knowledge", "vault_snapshot.json")
    os.makedirs(os.path.dirname(graph_path), exist_ok=True)
    with open(graph_path, "w") as f:
        json.dump(graph, f, indent=2)
    print(f"Vault snapshot saved to {graph_path}")


def run_graph_sync():
    graph = load_graph()
    print(f"Loaded graph: {len(graph.get('nodes', []))} nodes, {len(graph.get('edges', []))} edges")
    sync_nodes(graph)
    sync_edges(graph)
    vault_snapshot(graph)


if __name__ == "__main__":
    run_graph_sync()
