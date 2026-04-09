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
    "Jaden": ["jaden"],
    "Jeffery": ["jeffery", "jeffrey"],
    "The Boys": ["boys", "son", "sons"],
    "Solvstrat": ["solvstrat", "solv", "production team", "2.0"],
    "Crayon": ["crayon", "crayon biz"],
    "Church": ["church", "pastor", "pastor marcus", "marcus"],
    "₹30L Debt": ["debt", "loan", "loan(s)", "borrowed", "borrower", "financial", "money", "credit card", "₹", "rs.", "lakh", "lakhs"],
}

MEMORY_TYPE_MAPPING = {
    "Prophetic Word (From God or others)": "Prophecy",
    "Praise & Cries (My Psalm to God)": "Psalm",
    "Personal Thoughts / Journaling": "Journal",
    "Prayer / Intercession": "Prayer",
    "Sermon / Teaching": "Sermon",
}


def synthesize_content(entry_type: str, row: dict) -> str:
    topic = row.get("What is on your heart today?", "").strip()
    thoughts = row.get("Pour out your thoughts here.", "").strip()
    prayer = row.get("The Prayer Content", "").strip()
    word_received = row.get("What is the word received?", "").strip()
    psalm = row.get("Write your psalm to the Lord.", "").strip()
    key_takeaway = row.get("Key Takeaway or Lesson Learned?", "").strip()
    
    if entry_type == "Prophecy":
        parts = [f"[PROPHECY] {word_received}" if word_received else ""]
        if topic:
            parts.append(f"Topic: {topic}")
        if key_takeaway:
            parts.append(f"Lesson: {key_takeaway}")
        return " | ".join([p for p in parts if p])
    
    elif entry_type == "Psalm":
        parts = [f"[PSALM] {psalm}" if psalm else ""]
        if word_received:
            parts.append(f"Word: {word_received}")
        if key_takeaway:
            parts.append(f"Takeaway: {key_takeaway}")
        return " | ".join([p for p in parts if p])
    
    elif entry_type == "Prayer":
        parts = [f"[PRAYER] {prayer}" if prayer else ""]
        if topic:
            parts.append(f"Praying for: {topic}")
        return " | ".join([p for p in parts if p])
    
    elif entry_type == "Sermon":
        parts = [f"[SERMON] {thoughts}" if thoughts else ""]
        if word_received:
            parts.append(f"Revelation: {word_received}")
        if key_takeaway:
            parts.append(f"Application: {key_takeaway}")
        return " | ".join([p for p in parts if p])
    
    else:  # Journal
        parts = [thoughts] if thoughts else []
        if word_received:
            parts.append(f"Word: {word_received}")
        if key_takeaway:
            parts.append(f"Takeaway: {key_takeaway}")
        return " | ".join([p for p in parts if p])


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
    
    node_type = "person" if label in ["Sunju", "Jaden", "Jeffery", "The Boys"] else "organization" if label in ["Solvstrat", "Crayon", "Church"] else "concept"
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
    
    supabase.table("graph_edges").insert({
        "source_node_id": source_id,
        "target_node_id": target_id,
        "relationship": relationship,
        "metadata": json.dumps({"memory_id": memory_id})
    }).execute()


def check_duplicate(timestamp: str) -> bool:
    if not timestamp:
        return False
    existing = supabase.table("memories").select("id").eq("created_at", timestamp).execute()
    return len(existing.data) > 0


def graphify(text: str, memory_id: str):
    if not text:
        return
    text_lower = text.lower()
    entities = []
    
    for entity, keywords in ENTITY_MAPPINGS.items():
        for kw in keywords:
            if kw in text_lower:
                entities.append(entity)
                break
    entities = list(set(entities))
    
    if "Danny" not in entities and any(e in text_lower for e in ["i ", "my ", "me ", "i'm", "i am"]):
        pass
    
    create_edge("Danny", "Danny", "authored", memory_id)
    
    for entity in entities:
        if entity == "Sunju":
            create_edge("Danny", "Sunju", "relates_to", memory_id)
            create_edge("Sunju", "Danny", "relates_to", memory_id)
        elif entity in ["Jaden", "Jeffery", "The Boys"]:
            create_edge("Danny", entity, "parent_of", memory_id)
            create_edge(entity, "Danny", "child_of", memory_id)
        elif entity in ["Solvstrat", "Crayon"]:
            create_edge("Danny", entity, "works_at", memory_id)
            create_edge(entity, "Danny", "employs", memory_id)
        elif entity == "Church":
            create_edge("Danny", "Church", "belongs_to", memory_id)
        elif entity == "₹30L Debt":
            create_edge("Danny", "₹30L Debt", "struggles_with", memory_id)
    
    if "Sunju" in entities and "Solvstrat" in entities:
        create_edge("Sunju", "Solvstrat", "connected_via", memory_id)
    if "The Boys" in entities and "Sunju" in entities:
        create_edge("The Boys", "Sunju", "cared_by", memory_id)


def process_row(row: dict) -> dict:
    ts = row.get("Timestamp", "")
    created_at = parse_timestamp(ts)
    
    entry_type_raw = row.get("What is on your heart today?", "").strip()
    entry_type = MEMORY_TYPE_MAPPING.get(entry_type_raw, "Journal")
    
    content = synthesize_content(entry_type, row)
    
    emotional_state = row.get("Emotional State (Archived)", "").strip()
    if not emotional_state:
        emotional_state = row.get("Emotional State", "").strip()
    
    try:
        intensity = int(row.get("Emotional Intensity", "").strip() or 0)
    except:
        intensity = 0
    
    faith_score_raw = row.get("Faith Score", "").strip()
    try:
        faith_score = int(faith_score_raw) if faith_score_raw else 0
    except:
        faith_score = 0
    
    spillover_flag = row.get("Spillover Flag", "").strip()
    
    emotional_intensity = row.get("Emotional Intensity", "").strip()
    try:
        em_int = int(emotional_intensity) if emotional_intensity else 0
    except:
        em_int = 0
    
    metadata = {
        "emotional_state": emotional_state,
        "intensity": intensity,
        "faith_score": faith_score,
        "spillover_flag": spillover_flag,
        "emotional_intensity": em_int,
        "location": row.get("Where am I?", "").strip(),
        "category": row.get("Category", "").strip(),
        "tags": row.get("Tags or Themes?", "").strip(),
        "entry_type": entry_type,
        "source": "archive_ingest"
    }
    
    return {
        "created_at": created_at,
        "content": content,
        "memory_type": entry_type,
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
                graphify(parsed["content"], memory_id)
            
            inserted += 1
            if inserted % 10 == 0:
                print(f"Inserted {inserted} memories...")
                
        except Exception as e:
            print(f"Error inserting row: {e}")
            continue
    
    print(f"\nComplete: {inserted} inserted, {skipped} skipped (duplicates)")


if __name__ == "__main__":
    run_ingest()