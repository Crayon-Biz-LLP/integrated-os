import os
import json
from supabase import create_client, Client
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)

BATCH_SIZE = 10

ENTITY_MAPPINGS = {
    "Sunju": ["sunju", "wife", "wife's", "sunju's"],
    "Jaden": ["jaden"],
    "Jeffery": ["jeffery", "jeffrey"],
    "The Boys": ["boys", "son", "sons"],
    "Solvstrat": ["solvstrat", "solv", "production team", "2.0"],
    "Crayon": ["crayon", "crayon biz"],
    "Church": ["church", "pastor", "pastor marcus", "marcus"],
    "₹30L Debt": ["debt", "loan", "loan(s)", "borrowed", "borrower", "financial", "money", "credit card", "₹", "rs.", "lakh", "lakhs"],
}


def fetch_archive_memories():
    result = supabase.table("memories").select("id, content, created_at").eq("memory_type", "archive").execute()
    return result.data or []


def fetch_people_lookup():
    result = supabase.table("people").select("id, name").execute()
    return {row["name"]: row["id"] for row in (result.data or [])}


def extract_entities(text: str) -> list:
    if not text:
        return []
    text_lower = text.lower()
    found = []
    for entity, keywords in ENTITY_MAPPINGS.items():
        for kw in keywords:
            if kw in text_lower:
                found.append(entity)
                break
    return list(set(found))


def get_or_create_node(label: str, people_lookup: dict) -> str:
    if label in people_lookup:
        return people_lookup[label]
    
    existing = supabase.table("graph_nodes").select("id").eq("label", label).execute()
    if existing.data:
        return existing.data[0]["id"]
    
    node_type = "person" if label in ["Sunju", "Jaden", "Jeffery", "The Boys"] else "organization" if label in ["Solvstrat", "Crayon", "Church"] else "concept"
    resp = supabase.table("graph_nodes").insert({
        "label": label,
        "type": node_type,
        "metadata": json.dumps({"source": "backfill_graph"})
    }).execute()
    return resp.data[0]["id"] if resp.data else None


def create_edge(source_id: str, target_id: str, relationship: str, memory_id: str):
    if not source_id or not target_id:
        return
    supabase.table("graph_edges").insert({
        "source_node_id": source_id,
        "target_node_id": target_id,
        "relationship": relationship,
        "metadata": json.dumps({"memory_id": memory_id})
    }).execute()


def process_memory(memory: dict, people_lookup: dict):
    content = memory.get("content", "")
    memory_id = memory["id"]
    
    entities = extract_entities(content)
    
    if not entities:
        return
    
    danny_id = get_or_create_node("Danny", people_lookup)
    create_edge(danny_id, danny_id, "authored", memory_id)
    
    for entity in entities:
        entity_id = get_or_create_node(entity, people_lookup)
        
        if entity == "Sunju":
            create_edge(danny_id, entity_id, "relates_to", memory_id)
            create_edge(entity_id, danny_id, "relates_to", memory_id)
        elif entity in ["Jaden", "Jeffery", "The Boys"]:
            create_edge(danny_id, entity_id, "parent_of", memory_id)
            create_edge(entity_id, danny_id, "child_of", memory_id)
        elif entity in ["Solvstrat", "Crayon"]:
            create_edge(danny_id, entity_id, "works_at", memory_id)
            create_edge(entity_id, danny_id, "employs", memory_id)
        elif entity == "Church":
            create_edge(danny_id, entity_id, "belongs_to", memory_id)
        elif entity == "₹30L Debt":
            create_edge(danny_id, entity_id, "struggles_with", memory_id)
    
    if "Sunju" in entities and "Solvstrat" in entities:
        sunju_id = get_or_create_node("Sunju", people_lookup)
        solvstrat_id = get_or_create_node("Solvstrat", people_lookup)
        create_edge(sunju_id, solvstrat_id, "connected_via", memory_id)
    if "The Boys" in entities and "Sunju" in entities:
        boys_id = get_or_create_node("The Boys", people_lookup)
        sunju_id = get_or_create_node("Sunju", people_lookup)
        create_edge(boys_id, sunju_id, "cared_by", memory_id)


def run_backfill():
    print("Fetching archive memories...")
    memories = fetch_archive_memories()
    print(f"Found {len(memories)} archive memories")
    
    print("Building people lookup...")
    people_lookup = fetch_people_lookup()
    print(f"Found {len(people_lookup)} people")
    
    for i in range(0, len(memories), BATCH_SIZE):
        batch = memories[i:i + BATCH_SIZE]
        print(f"Processing batch {i // BATCH_SIZE + 1} ({len(batch)} memories)...")
        
        for memory in batch:
            try:
                process_memory(memory, people_lookup)
            except Exception as e:
                print(f"Error processing memory {memory['id']}: {e}")
        
        print(f"Completed batch {i // BATCH_SIZE + 1}")
    
    print("Backfill complete!")


if __name__ == "__main__":
    run_backfill()