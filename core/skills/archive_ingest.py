import os
import json
from datetime import datetime
from supabase import create_client, Client
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
load_dotenv(dotenv_path)

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(supabase_url, supabase_key)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

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


def get_google_creds():
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )


def get_sheets_service():
    return build('sheets', 'v4', credentials=get_google_creds())


def fetch_sheet_data():
    if not GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID not set")
    
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range='Form responses 1!A:AI'
    ).execute()
    
    values = result.get('values', [])
    if not values:
        return []
    
    return values[1:]


def synthesize_content(entry_type: str, row) -> str:
    is_list = isinstance(row, list)
    
    if is_list:
        topic = row[2].strip() if len(row) > 2 else ""
        thoughts = row[6].strip() if len(row) > 6 else ""
        prayer = row[7].strip() if len(row) > 7 else ""
        word_received = row[5].strip() if len(row) > 5 else ""
        psalm = row[4].strip() if len(row) > 4 else ""
        key_takeaway = row[19].strip() if len(row) > 19 else ""
    else:
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
    
    else:
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


def process_row(row) -> dict:
    is_list = isinstance(row, list)
    
    ts = row[0] if is_list else row.get("Timestamp", "")
    created_at = parse_timestamp(ts)
    
    if is_list:
        entry_type_raw = row[2].strip() if len(row) > 2 else ""
    else:
        entry_type_raw = row.get("What is on your heart today?", "").strip()
    entry_type = MEMORY_TYPE_MAPPING.get(entry_type_raw, "Journal")
    
    content = synthesize_content(entry_type, row)
    
    if is_list:
        emotional_state = row[22].strip() if len(row) > 22 else ""
    else:
        emotional_state = row.get("Emotional State (Archived)", "").strip()
        if not emotional_state:
            emotional_state = row.get("Emotional State", "").strip()
    
    intensity = 0
    faith_score = 0
    spillover_flag = ""
    em_int = 0
    
    if is_list:
        try:
            intensity = int(row[21]) if len(row) > 21 and row[21] else 0
        except:
            intensity = 0
        try:
            faith_score = int(row[30]) if len(row) > 30 and row[30] else 0
        except:
            faith_score = 0
        spillover_flag = row[29].strip() if len(row) > 29 else ""
        try:
            em_int = int(row[21]) if len(row) > 21 and row[21] else 0
        except:
            em_int = 0
        category = row[28].strip() if len(row) > 28 else ""
        action_velocity = row[31].strip() if len(row) > 31 else ""
        consistency_score = row[32].strip() if len(row) > 32 else ""
        victory_flag = row[33].strip() if len(row) > 33 else ""
        input_score = row[34].strip() if len(row) > 34 else ""
        location = row[17].strip() if len(row) > 17 else ""
        tags = row[20].strip() if len(row) > 20 else ""
    else:
        try:
            intensity = int(row.get("Emotional Intensity", "").strip() or 0)
        except:
            intensity = 0
        try:
            faith_score = int(row.get("Faith Score", "").strip() or 0)
        except:
            faith_score = 0
        spillover_flag = row.get("Spillover Flag", "").strip()
        try:
            em_int = int(row.get("Emotional Intensity", "").strip() or 0)
        except:
            em_int = 0
        category = row.get("Category", "").strip()
        action_velocity = row.get("Action Velocity", "").strip()
        consistency_score = row.get("Consistency Score", "").strip()
        victory_flag = row.get("Victory Flag", "").strip()
        input_score = row.get("Input Score", "").strip()
        location = row.get("Where am I?", "").strip()
        tags = row.get("Tags or Themes?", "").strip()
    
    metadata = {
        "emotional_state": emotional_state,
        "intensity": intensity,
        "faith_score": faith_score,
        "spillover_flag": spillover_flag,
        "emotional_intensity": em_int,
        "location": location,
        "category": category,
        "tags": tags,
        "entry_type": entry_type,
        "source": "archive_ingest",
        "action_velocity": action_velocity,
        "consistency_score": consistency_score,
        "victory_flag": victory_flag,
        "input_score": input_score,
    }
    
    return {
        "created_at": created_at,
        "content": content,
        "memory_type": entry_type,
        "metadata": json.dumps(metadata)
    }


def get_last_sync_time() -> str:
    result = supabase.table("memories").select("created_at").eq("memory_type", "archive").order("created_at", desc=True).limit(1).execute()
    if result.data:
        return result.data[0]["created_at"]
    return None


def run_ingest():
    if not GOOGLE_SHEET_ID:
        print("GOOGLE_SHEET_ID not set, skipping archive ingest")
        return
    
    last_sync = get_last_sync_time()
    print(f"Last archive sync: {last_sync or 'None (initial run)'}")
    
    rows = fetch_sheet_data()
    print(f"Fetched {len(rows)} rows from Google Sheet")
    
    inserted = 0
    skipped = 0
    
    for row in rows:
        parsed = process_row(row)
        
        if not parsed["created_at"]:
            print(f"Skipping row with no valid timestamp")
            continue
        
        if last_sync and parsed["created_at"] <= last_sync:
            skipped += 1
            continue
        
        if check_duplicate(parsed["created_at"]):
            skipped += 1
            continue
        
        try:
            result = supabase.table("memories").insert({
                "created_at": parsed["created_at"],
                "content": parsed["content"],
                "memory_type": "archive",
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
    
    print(f"\nComplete: {inserted} inserted, {skipped} skipped (incremental + duplicates)")


if __name__ == "__main__":
    run_ingest()