import os
import csv
import json
from datetime import datetime
from supabase import create_client, Client
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)

CSV_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'archive', "Danny's Journal - Form responses 1.csv")

ENTITY_MAPPINGS = {
    "Sunju": ["sunju", "wife", "wife's", "sunju's"],
    "The Boys": ["jaden", "jeffery", "jeffrey", "boys", "son", "sons"],
    "Solvstrat": ["solvstrat", "solv", "production team", "2.0"],
    "₹30L Debt": ["debt", "loan", "loan(s)", "borrowed", "borrower", "financial", "money", "credit card", "₹", "rs.", "lakh", "lakhs"],
}


def parse_timestamp(ts: str) -> str:
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts.strip(), "%d/%m/%Y %H:%M:%S")
        return dt.isoformat()
    except:
        try:
            dt = datetime.strptime(ts.strip(), "%d/%m/%Y")
            return dt.isoformat()
        except:
            return None


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


def ensure_node(label: str) -> str:
    existing = supabase.table("graph_nodes").select("id").eq("label", label).execute()
    if existing.data:
        return existing.data[0]["id"]
    
    node_type = "person" if label in ["Sunju", "The Boys"] else "organization" if label == "Solvstrat" else "concept"
    resp = supabase.table("graph_nodes").insert({
        "label": label,
        "type": node_type,
        "metadata": json.dumps({"source": "archive_ingest"})
    }).execute()
    return resp.data[0]["id"] if resp.data else None


def create_edge(source_label: str, target_label: str, relationship: str, memory_id: str):
    source_id = ensure_node(source_label)
    target_id = ensure_node(target_label)
    if not source_id or not target_id:
        return
    
    supabase.table("graph_edges").upsert({
        "source_node_id": source_id,
        "target_node_id": target_id,
        "relationship": relationship,
        "metadata": json.dumps({"memory_id": memory_id})
    }, on_conflict="source_node_id_target_node_id").execute()


def check_duplicate(timestamp: str) -> bool:
    if not timestamp:
        return False
    existing = supabase.table("memories").select("id").eq("created_at", timestamp).execute()
    return len(existing.data) > 0


def process_row(row: dict) -> dict:
    ts = row.get("Timestamp", "")
    created_at = parse_timestamp(ts)
    
    topic = row.get("What is the topic on?", "").strip()
    thoughts = row.get("Pour out your thoughts here.", "").strip()
    prayer = row.get("The Prayer Content", "").strip()
    word_received = row.get("What is the word received?", "").strip()
    
    content_parts = [topic, thoughts, prayer, word_received]
    content = " | ".join([p for p in content_parts if p])
    
    emotional_state = row.get("Emotional State", "").strip()
    intensity = row.get("Emotional Intensity", "").strip()
    
    metadata = {
        "emotional_state": emotional_state,
        "intensity": intensity,
        "location": row.get("Where am I?", "").strip(),
        "category": row.get("Category", "").strip(),
        "tags": row.get("Tags or Themes?", "").strip(),
    }
    
    return {
        "created_at": created_at,
        "content": content,
        "memory_type": "archive",
        "metadata": json.dumps(metadata)
    }


def run_ingest():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return
    
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    print(f"Processing {len(rows)} rows...")
    
    inserted = 0
    skipped = 0
    
    for row in rows:
        parsed = process_row(row)
        
        if not parsed["created_at"]:
            print(f"Skipping row with no valid timestamp")
            continue
        
        if check_duplicate(parsed["created_at"]):
            skipped += 1
            continue
        
        try:
            result = supabase.table("memories").insert({
                "created_at": parsed["created_at"],
                "content": parsed["content"],
                "memory_type": parsed["memory_type"],
                "metadata": parsed["metadata"]
            }).execute()
            
            memory_id = result.data[0]["id"] if result.data else None
            
            if memory_id:
                entities = extract_entities(parsed["content"])
                for entity in entities:
                    create_edge("Danny", entity, "mentioned_in", memory_id)
            
            inserted += 1
            if inserted % 10 == 0:
                print(f"Inserted {inserted} memories...")
                
        except Exception as e:
            print(f"Error inserting row: {e}")
            continue
    
    print(f"\nComplete: {inserted} inserted, {skipped} skipped (duplicates)")


if __name__ == "__main__":
    run_ingest()