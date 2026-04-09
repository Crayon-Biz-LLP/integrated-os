import os
import json
from supabase import create_client, Client
from dotenv import load_dotenv
from google import genai

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
gemini_api_key = os.getenv("GEMINI_API_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)
gemini_client = genai.Client(api_key=gemini_api_key)

BATCH_SIZE = 10
MEMORY_TYPES = ["Prophecy", "Psalm", "Prayer", "Journal", "archive"]


def fetch_memories():
    result = supabase.table("memories").select(
        "id, content, memory_type, metadata, created_at"
    ).in_("memory_type", MEMORY_TYPES).execute()
    return result.data or []


def fetch_people_lookup():
    result = supabase.table("people").select("id, name").execute()
    return {row["name"]: row["id"] for row in (result.data or [])}


def synthesize_content(memory: dict) -> str:
    memory_type = memory.get("memory_type", "")
    content = memory.get("content", "")
    metadata = memory.get("metadata", {})
    
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except:
            metadata = {}
    
    if memory_type == "Prophecy":
        entry_type = metadata.get("entry_type", "")
        return f"[PROPHECY:{entry_type}] {content}" if entry_type else content
    
    elif memory_type in ["Psalm", "Prayer"]:
        tags = metadata.get("tags", "")
        if tags:
            return f"[TAGS:{tags}] {content}"
        return content
    
    return content


def extract_graph_elements(text: str, memory_id: str) -> dict:
    prompt = f"""Extract knowledge graph elements from this text.

Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "person"|"organization"|"project"|"emotional_state"}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string}}

Text: {text}

Rules:
- Extract People (names), Organizations, Projects, Emotional States as nodes
- Create edges for relationships between nodes
- Use clear, simple relationship types (e.g., "relates_to", "parent_of", "works_at", "belongs_to", "authored")
- Include "authored" edge from "Danny" to indicate he wrote this memory"""

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        
        if hasattr(response, 'text') and response.text:
            return json.loads(response.text)
        return {"nodes": [], "edges": []}
    except Exception as e:
        print(f"Graph extraction error: {e}")
        return {"nodes": [], "edges": []}


def get_or_create_node(label: str, people_lookup: dict, created_nodes: dict, memory_id: str = None) -> str:
    if label in created_nodes:
        return created_nodes[label]
    
    if label in people_lookup:
        created_nodes[label] = people_lookup[label]
        return people_lookup[label]
    
    existing = supabase.table("graph_nodes").select("id").eq("label", label).execute()
    if existing.data:
        node_id = existing.data[0]["id"]
        created_nodes[label] = node_id
        return node_id
    
    node_type = "person" if label in ["Sunju", "Jaden", "Jeffery", "The Boys", "Danny"] else \
                "organization" if label in ["Solvstrat", "Crayon", "Church"] else \
                "project" if label in ["CashFlow+", "Integrated-OS"] else "concept"
    
    resp = supabase.table("graph_nodes").insert({
        "label": label,
        "type": node_type,
        "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})
    }).execute()
    
    node_id = resp.data[0]["id"] if resp.data else None
    if node_id:
        created_nodes[label] = node_id
    return node_id


def upsert_nodes(nodes: list, people_lookup: dict, memory_id: str):
    if not nodes:
        return
    
    node_records = []
    for node in nodes:
        label = node.get("label", "")
        node_type = node.get("type", "concept")
        
        existing_id = people_lookup.get(label)
        if existing_id:
            node_records.append({
                "id": existing_id,
                "label": label,
                "type": node_type,
                "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})
            })
        else:
            node_records.append({
                "label": label,
                "type": node_type,
                "on_conflict": "label",
                "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})
            })
    
    if node_records:
        try:
            supabase.table("graph_nodes").upsert(
                node_records,
                on_conflict="label"
            ).execute()
        except Exception as e:
            print(f"Node upsert error: {e}")


def insert_edges(edges: list, node_label_to_id: dict, memory_id: str):
    for edge in edges:
        source_label = edge.get("source", "")
        target_label = edge.get("target", "")
        relationship = edge.get("relationship", "relates_to")
        
        source_id = node_label_to_id.get(source_label)
        target_id = node_label_to_id.get(target_label)
        
        if not source_id or not target_id:
            continue
        
        supabase.table("graph_edges").insert({
            "source_node_id": source_id,
            "target_node_id": target_id,
            "relationship": relationship,
            "metadata": json.dumps({"memory_id": memory_id})
        }).execute()


def process_memory(memory: dict, people_lookup: dict) -> bool:
    memory_id = memory["id"]
    synthesized = synthesize_content(memory)
    
    if not synthesized.strip():
        return False
    
    graph_data = extract_graph_elements(synthesized, memory_id)
    
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    
    if not nodes and not edges:
        return False
    
    created_nodes = {}
    
    for node in nodes:
        label = node.get("label", "")
        if label in people_lookup:
            created_nodes[label] = people_lookup[label]
    
    upsert_nodes(nodes, people_lookup, memory_id)
    
    node_label_to_id = {}
    for node in nodes:
        label = node.get("label", "")
        node_id = get_or_create_node(label, people_lookup, created_nodes, memory_id)
        if node_id:
            node_label_to_id[label] = node_id
    
    if not node_label_to_id:
        danny_id = get_or_create_node("Danny", people_lookup, created_nodes)
        if danny_id:
            node_label_to_id["Danny"] = danny_id
    
    insert_edges(edges, node_label_to_id, memory_id)
    
    return True


def run_backfill():
    print("Fetching memories...")
    memories = fetch_memories()
    print(f"Found {len(memories)} memories")
    
    print("Building people lookup...")
    people_lookup = fetch_people_lookup()
    print(f"Found {len(people_lookup)} people")
    
    processed = 0
    failed = 0
    
    for i in range(0, len(memories), BATCH_SIZE):
        batch = memories[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"Processing batch {batch_num} ({len(batch)} memories)...")
        
        for memory in batch:
            try:
                success = process_memory(memory, people_lookup)
                if success:
                    processed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"Error processing memory {memory['id']}: {e}")
                failed += 1
        
        print(f"Completed batch {batch_num}")
    
    print(f"Backfill complete! Processed: {processed}, Skipped: {failed}")


if __name__ == "__main__":
    run_backfill()