import os
import json
import time
import uuid
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

BATCH_SIZE = 20
MEMORY_TYPES = ["Prophecy", "Psalm", "Prayer", "Journal", "archive"]


def with_retry(fn, retries=3, base_delay=1, label="operation"):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                wait = base_delay * (2 ** attempt)
                print(f"{label} failed (attempt {attempt+1}/3), retrying in {wait}s... Error: {e}")
                time.sleep(wait)
            else:
                print(f"{label} failed after 3 attempts: {e}")
                raise e


def fetch_all_paginated(table_name: str, select_str: str = "*", in_filter_col=None, in_filter_val=None):
    all_rows = []
    start = 0
    page_size = 1000
    while True:
        query = supabase.table(table_name).select(select_str)
        if in_filter_col and in_filter_val:
            query = query.in_(in_filter_col, in_filter_val)
        
        try:
            res = with_retry(
                lambda: query.range(start, start + page_size - 1).execute(),
                label="Paginated fetch"
            )
            data = res.data or []
        except Exception:
            break
        
        all_rows.extend(data)
        
        if len(data) < page_size:
            break
        start += page_size
    return all_rows


def fetch_memories():
    existing_edges = fetch_all_paginated("graph_edges", "metadata")
    processed_memory_ids = set()
    for row in existing_edges or []:
        try:
            meta = json.loads(row.get("metadata", "{}"))
            if meta.get("memory_id"):
                processed_memory_ids.add(meta["memory_id"])
        except:
            pass
    
    memories = fetch_all_paginated("memories", "id, content, memory_type, metadata, created_at", "memory_type", MEMORY_TYPES)
    
    memories = [m for m in (memories or []) if m["id"] not in processed_memory_ids]
    
    known_entities = fetch_known_entities()

    raw_dumps = fetch_all_paginated("raw_dumps", "id, content, created_at")
    qualifying_dumps = [
        {
            "id": d["id"],
            "content": d.get("content", ""),
            "memory_type": "raw_dump",
            "metadata": {},
            "created_at": d.get("created_at")
        }
        for d in (raw_dumps or [])
        if d["id"] not in processed_memory_ids
        and dump_contains_known_entity(d.get("content", ""), known_entities)
    ]
    memories = memories + qualifying_dumps

    return memories
    

def fetch_graph_entities():
    nodes = fetch_all_paginated("graph_nodes", "id, label, type, metadata")
    return {row["label"]: {"id": row["id"], "type": row["type"]} for row in (nodes or [])}

def fetch_known_entities() -> set:
    nodes = fetch_all_paginated("graph_nodes", "label, type")
    return {
        row["label"].lower()
        for row in (nodes or [])
        if row["type"] in ("person", "organization", "project")
    }

def dump_contains_known_entity(content: str, known_entities: set) -> bool:
    content_lower = content.lower()
    return any(entity in content_lower for entity in known_entities)

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


def gemini_with_retry_sync(prompt: str, model: str, config: dict = None, retries: int = 3, base_delay: int = 2):
    retryable_errors = ['503', '504', '500', 'timeout', 'deadline exceeded']
    for attempt in range(retries):
        try:
            return gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=config or {}
            )
        except Exception as e:
            error_str = str(e).lower()
            should_retry = any(err in error_str for err in retryable_errors)
            if should_retry and attempt < retries - 1:
                wait = base_delay * (2 ** attempt)
                print(f"Gemini retry {attempt+1}/{retries} in {wait}s: {e}")
                time.sleep(wait)
                continue
            raise


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
        response = gemini_with_retry_sync(
            prompt=prompt,
            model="gemini-3.1-flash-lite-preview",
            config={"response_mime_type": "application/json"}
        )
        
        if hasattr(response, 'text') and response.text:
            return json.loads(response.text)
        return {"nodes": [], "edges": []}
    except Exception as e:
        print(f"Graph extraction error: {e}")
        return {"nodes": [], "edges": []}

def get_or_create_node(label: str, graph_entities: dict, created_nodes: dict, memory_id: str = None) -> str:
    if label in created_nodes:
        return created_nodes[label]

    if label in graph_entities:
        node_id = graph_entities[label]["id"]
        created_nodes[label] = node_id
        return node_id

    node_type = "concept"

    try:
        supabase.table("graph_nodes").upsert(
            {"label": label, "type": node_type},
            on_conflict="label",
            ignore_duplicates=True
        ).execute()
    except Exception as e:
        print(f"Node upsert warning for '{label}': {e}")

    try:
        res = supabase.table("graph_nodes") \
            .select("id") \
            .eq("label", label) \
            .single() \
            .execute()
        node_id = res.data["id"] if res.data else None
    except Exception as e:
        print(f"Node fetch failed for '{label}': {e}")
        return None

    if node_id:
        created_nodes[label] = node_id
    return node_id

def upsert_nodes(nodes: list, graph_entities: dict, memory_id: str):
    if not nodes:
        return
    
    node_records = []
    for node in nodes:
        label = node.get("label", "")
        node_type = node.get("type", "concept")
        
        # Start the record with all known data
        record = {
            "label": label,
            "type": node_type,
            "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})
        }
        
        existing_id = graph_entities.get(label, {}).get("id")
        if existing_id:
            record["id"] = existing_id
        else:
            # 🛡️ THE FIX: Generate a UUID for new nodes here.
            # This ensures the batch never contains 'null' IDs.
            record["id"] = str(uuid.uuid4())
            
        node_records.append(record)
    
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
        
        try:
            supabase.table("graph_edges").upsert({
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relationship": relationship,
                "metadata": json.dumps({"memory_id": memory_id})
            }, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
        except Exception as e:
            print(f"Edge insert failed ({source_label} -> {target_label}): {e}")
            continue


def process_memory(memory: dict, graph_entities: dict) -> bool:
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
        if label in graph_entities:
            created_nodes[label] = graph_entities[label]["id"]
    
    node_label_to_id = {}
    for node in nodes:
        label = node.get("label", "")
        node_id = get_or_create_node(label, graph_entities, created_nodes, memory_id)
        if node_id:
            node_label_to_id[label] = node_id
    
    if not node_label_to_id:
        danny_id = get_or_create_node("Danny", graph_entities, created_nodes)
        if danny_id:
            node_label_to_id["Danny"] = danny_id
    
    insert_edges(edges, node_label_to_id, memory_id)
    
    return True


def run_backfill():
    print("Fetching memories...")
    memories = fetch_memories()
    print(f"Found {len(memories)} memories")
    
    print("Building graph entities lookup...")
    graph_entities = fetch_graph_entities()
    print(f"Found {len(graph_entities)} entities (people + projects)")
    
    processed = 0
    failed = 0
    
    for i in range(0, len(memories), BATCH_SIZE):
        batch = memories[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"Processing batch {batch_num} ({len(batch)} memories)...")
        
        for memory in batch:
            try:
                success = process_memory(memory, graph_entities)
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