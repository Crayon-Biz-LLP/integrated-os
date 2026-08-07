from core.llm.compat import get_embedding_sync
import os
import time
import json
from datetime import datetime
from googleapiclient.errors import HttpError

from core.retrieval.pipeline import schedule_index_memory
from core.services.db import channel_tenant_scope, tenant_aware_client
from core.services.google_service import get_cached_service
from core.llm.retry import get_jittered_backoff
from core.lib.graph_rules import normalize_label
from core.lib.time_utils import get_user_timezone

supabase = tenant_aware_client()

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Tenant #1 (Danny) entity→keyword mappings — the SINGLE source of truth.
# Tenant #1 (Danny) entity mappings — kept verbatim so that
# scripts/seed_tenant1_m6_config.py can re-seed his row exactly. NOT used as
# a runtime fallback for other tenants (they start neutral via
# bootstrap_tenant.py).
TENANT1_ENTITY_MAPPINGS = {
    "Jaden": ["jaden"],
    "Qhord": ["qhord", "joel", "GTM"],
    "Sunju": ["sunju", "wife", "wife's", "sunju's"],
    "Church": ["church", "pastor", "pastor marcus", "marcus"],
    "Crayon": ["crayon", "crayon biz"],
    "Jeremy": ["jeremy"],
    "Jeffery": ["jeffery", "jeffrey"],
    "The Boys": ["boys", "son", "sons"],
    "Solvstrat": ["solvstrat", "solv", "production team", "2.0"],
}


def get_entity_mappings() -> dict:
    """Per-tenant entity→keyword mappings (M6): core_config 'entity_mappings'
    row, falling back to an EMPTY mapping (neutral) when unset — never
    another tenant's world. Read at call time so a tenant's config edits
    apply immediately and no cross-tenant value is ever cached in a
    module-level constant.
    """
    try:
        res = supabase.table('core_config').select('content').eq('key', 'entity_mappings').execute()
        if res.data and res.data[0].get('content'):
            content = res.data[0]['content']
            if isinstance(content, str):
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    print("⚠️ Failed to parse dynamic mappings JSON")
            elif isinstance(content, dict):
                return content
    except Exception as e:
        print(f"⚠️ Failed to fetch dynamic mappings: {e}")

    # Neutral fallback (no cross-tenant data): a tenant without an
    # 'entity_mappings' row gets no keyword mappings rather than another
    # tenant's world. Danny's own mapping lives in HIS core_config row
    # (seeded by scripts/seed_tenant1_m6_config.py).
    return {}

# ── Per-tenant graph rules (M6 de-personalization) ──────────────────────────
# archive_ingest used to hardcode Danny's family/orgs/₹30L debt and the root
# person label. Now read from core_config per-tenant. New tenants start
# neutral (bootstrap_tenant.py seeds empty rows); Danny's values live in HIS
# core_config row (seeded by scripts/seed_tenant1_m6_config.py). Keys:
# 'archive_person_labels', 'archive_org_labels', 'archive_edge_rules',
# 'archive_root_label'.

# Tenant #1 (Danny) default values — used ONLY to seed Danny's core_config
# row via scripts/seed_tenant1_m6_config.py. Never a runtime fallback for
# other tenants (they start neutral via bootstrap_tenant.py).
TENANT1_ARCHIVE_PERSON_LABELS = ["Danny", "Sunju", "Jaden", "Jeffery", "The Boys"]
TENANT1_ARCHIVE_ORG_LABELS = ["Solvstrat", "Crayon", "Church"]
TENANT1_ARCHIVE_EDGE_RULES = {
    "Sunju": [["{root}", "Sunju", "relates_to"], ["Sunju", "{root}", "relates_to"]],
    "Jaden": [["{root}", "Jaden", "parent_of"], ["Jaden", "{root}", "child_of"]],
    "Jeffery": [["{root}", "Jeffery", "parent_of"], ["Jeffery", "{root}", "child_of"]],
    "The Boys": [["{root}", "The Boys", "parent_of"], ["The Boys", "{root}", "child_of"]],
    "Solvstrat": [["{root}", "Solvstrat", "works_at"], ["Solvstrat", "{root}", "employs"]],
    "Crayon": [["{root}", "Crayon", "works_at"], ["Crayon", "{root}", "employs"]],
    "Church": [["{root}", "Church", "belongs_to"]],
    "₹30L Debt": [["{root}", "₹30L Debt", "struggles_with"]],
}
TENANT1_ARCHIVE_ROOT_LABEL = "Danny"


def _get_config_str(key: str) -> str | None:
    try:
        res = supabase.table('core_config').select('content').eq('key', key).execute()
        if res.data and res.data[0].get('content'):
            c = res.data[0]['content']
            return c if isinstance(c, str) else (json.dumps(c) if isinstance(c, (dict, list)) else str(c))
    except Exception:
        pass
    return None


def _get_config_json(key: str, default):
    raw = _get_config_str(key)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return default


def resolve_root_label() -> str | None:
    """The tenant's root person label (their own name), or None if unknown.

    Resolution (M6): core_config 'archive_root_label' (admin override) →
    user_settings name → None. No hardcoded fallback — a tenant without a
    resolvable root simply gets no root-anchored edges.
    """
    try:
        cfg = _get_config_str("archive_root_label")
        if cfg and cfg.strip():
            return cfg.strip()
    except Exception:
        pass
    try:
        from core.services.user_settings import resolve_user_name, current_user_id
        name = resolve_user_name(current_user_id())
        if name:
            return name
    except Exception:
        pass
    return None


def person_labels() -> list[str]:
    """Person entity labels for node typing (per-tenant config; neutral default)."""
    return _get_config_json("archive_person_labels", [])


def org_labels() -> list[str]:
    """Organization entity labels for node typing (per-tenant config; neutral default)."""
    return _get_config_json("archive_org_labels", [])


def edge_rules() -> dict:
    """entity → list of (source, target, relationship) edge specs. '{root}'
    in source/target is replaced with the tenant's root person label.

    Per-tenant config; neutral default (no edges) when unset.
    """
    return _get_config_json("archive_edge_rules", {})


MEMORY_TYPE_MAPPING = {
    "Prophetic Word (From God or others)": "Prophecy",
    "Praise & Cries (My Psalm to God)": "Psalm",
    "Personal Thoughts / Journaling": "Journal",
    "Prayer / Intercession": "Prayer",
    "Sermon / Teaching": "Sermon",
}

def with_retry(fn, retries=3, base_delay=1, label="operation"):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                wait = get_jittered_backoff(attempt, base_delay)
                print(f"{label} failed (attempt {attempt+1}/{retries}), retrying in {wait:.1f}s... Error: {e}")
                time.sleep(wait)
            else:
                print(f"{label} failed after {retries} attempts: {e}")
                raise e



def get_sheets_service():
    service = get_cached_service('sheets', 'v4')
    if service is None:
        raise ValueError("No Google creds for this tenant (M5) — archive ingest requires Google Sheets access")
    return service


def fetch_sheet_data():
    """Fetch all data from the Google Sheet with exponential backoff."""
    if not GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID not set")
    
    SHEET_NAME = 'Form responses 1'
    range_name = f"{SHEET_NAME}!A:AI"
    service = get_sheets_service()
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"📡 Fetching data from Google Sheets (Attempt {attempt + 1})...")
            result = service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=range_name
            ).execute()
            values = result.get('values', [])
            if not values:
                return []
            return values[1:]
            
        except HttpError as e:
            if e.resp.status in [500, 503, 504] and attempt < max_retries - 1:
                wait_time = get_jittered_backoff(attempt)
                print(f"⚠️ Google service busy (503). Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ Permanent Sheets API Error: {e}")
                raise
    return []


def synthesize_content(entry_type: str, row) -> str:
    thoughts = row[4].strip() if len(row) > 4 and row[4] else ""
    takeaway = row[5].strip() if len(row) > 5 and row[5] else ""
    word = row[6].strip() if len(row) > 6 and row[6] else ""
    psalm = row[10].strip() if len(row) > 10 and row[10] else ""
    testimony = row[12].strip() if len(row) > 12 and row[12] else ""
    action = row[15].strip() if len(row) > 15 and row[15] else ""
    prayer = row[19].strip() if len(row) > 19 and row[19] else ""
    sermon = row[24].strip() if len(row) > 24 and row[24] else ""

    parts = []
    
    if entry_type == "Psalm" and psalm:
        parts.append(f"[PSALM] {psalm}")
    elif entry_type == "Prayer" and prayer:
        parts.append(f"[PRAYER] {prayer}")
    elif entry_type == "Sermon" and sermon:
        parts.append(f"[SERMON] {sermon}")
    elif entry_type == "Prophecy" and word:
        parts.append(f"[PROPHECY] {word}")
    elif thoughts:
        parts.append(thoughts)
    else:
        return ""

    if word and entry_type != "Prophecy":
        parts.append(f"Word: {word}")
    if takeaway:
        parts.append(f"Takeaway: {takeaway}")
    if action:
        parts.append(f"Action: {action}")
    if testimony:
        parts.append(f"Testimony: {testimony}")

    return " | ".join([p for p in parts if p])



def parse_timestamp(ts: str) -> str:
    """Parse the archive sheet timestamp in the tenant's timezone (M6:
    per-tenant via get_user_timezone, IST default for legacy/tenant #1).
    """
    if not ts:
        return None
    tz = get_user_timezone()
    try:
        dt = datetime.strptime(ts.strip(), "%d/%m/%Y %H:%M:%S")
        dt = dt.replace(tzinfo=tz)
        return dt.isoformat()
    except Exception:
        try:
            dt = datetime.strptime(ts.strip(), "%d/%m/%Y")
            dt = dt.replace(tzinfo=tz)
            return dt.isoformat()
        except Exception:
            return None


def ensure_node(label: str) -> str:
    # Per-tenant typing (M6): config-driven person/org lists, Danny fallback.
    _label = label.lower().strip()
    _person = {p.lower() for p in person_labels()}
    _org = {o.lower() for o in org_labels()}
    if _label in _person:
        node_type = "person"
    elif _label in _org:
        node_type = "organization"
    else:
        node_type = "concept"
    existing = with_retry(
        lambda: supabase.table("graph_nodes").select("id, canonical_id").ilike("label", label).eq('is_current', True).execute(),
        label="Node select"
    )
    if existing.data:
        from core.lib.graph_rules import get_canonical_id
        return get_canonical_id(existing.data[0]["id"])
    
    try:
        resp = with_retry(
            lambda: supabase.table("graph_nodes").insert({
                "label": label,
                "type": node_type,
                "normalized_label": normalize_label(label),
                "metadata": {"source": "archive_ingest"}
            }).execute(),
            label="Node insert"
        )
    except Exception:
        return None
    return resp.data[0]["id"] if resp.data else None


def create_edge(source_label: str, target_label: str, relationship: str, memory_id: str):
    source_id = ensure_node(source_label)
    target_id = ensure_node(target_label)
    if not source_id or not target_id:
        return
    
    try:
        with_retry(
            lambda: supabase.table("graph_edges").insert({
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relationship": relationship,
                "metadata": {"memory_id": memory_id}
            }).execute(),
            label="Edge insert"
        )
    except Exception as e:
        print(f"Edge insert error: {e}")
        return


def check_duplicate(timestamp: str, content: str) -> bool:
    if not timestamp or not content:
        return False
    try:
        content_snippet = content[:100].strip()
        existing = supabase.table("memories").select("id") \
            .eq("created_at", timestamp) \
            .execute()
        if existing.data:
            return True
        content_check = supabase.table("memories").select("id") \
            .ilike("content", f"{content_snippet}%") \
            .execute()
        return len(content_check.data) > 0
    except Exception as e:
        print(f"Duplicate check failed: {e}")
        return False


def graphify(text: str, memory_id: str, mappings: dict | None = None):
    """Create graph edges from archive text. `mappings` is the per-tenant
    entity→keyword map — pass it once per run (run_ingest) to avoid a DB
    read per row; when omitted it is fetched here (self-sufficient).
    """
    if not text:
        return
    text_lower = text.lower()
    entities = []

    if mappings is None:
        mappings = get_entity_mappings()
    for entity, keywords in mappings.items():
        for kw in keywords:
            if kw in text_lower:
                entities.append(entity)
                break
    entities = list(set(entities))
    
    # M6 de-personalization: edge rules are per-tenant config (default =
    # Danny's world for legacy/tenant #1). '{root}' is the tenant's own
    # person label — never a hardcoded name.
    root = resolve_root_label()
    rules = edge_rules()
    for entity in entities:
        for spec in rules.get(entity, []):
            if len(spec) < 3:
                continue
            src, tgt, rel = spec[0], spec[1], spec[2]
            src = root if src == "{root}" else src
            tgt = root if tgt == "{root}" else tgt
            if not src or not tgt:
                continue  # root-anchored edge but no root label resolvable
            create_edge(src, tgt, rel, memory_id)


def process_row(row) -> dict:
    is_list = isinstance(row, list)
    
    ts = row[0] if is_list else row.get("Timestamp", "")
    created_at = parse_timestamp(ts)
    
    if is_list:
        entry_type_raw = row[3].strip() if len(row) > 3 else ""
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
            intensity = int(row[14]) if len(row) > 14 and row[14] else 0
        except Exception:
            intensity = 0
        try:
            faith_score = int(row[30]) if len(row) > 30 and row[30] else 0
        except Exception:
            faith_score = 0
        spillover_flag = row[29].strip() if len(row) > 29 else ""
        try:
            em_int = int(row[21]) if len(row) > 21 and row[21] else 0
        except Exception:
            em_int = 0
        category = row[28].strip() if len(row) > 28 and row[28] else ""
        action_velocity = row[31].strip() if len(row) > 31 and row[31] else ""
        consistency_score = row[32].strip() if len(row) > 32 and row[32] else ""
        victory_flag = row[33].strip() if len(row) > 33 and row[33] else ""
        input_score = row[34].strip() if len(row) > 34 and row[34] else ""
        location = row[2].strip() if len(row) > 2 and row[2] else ""
        tags = row[16].strip() if len(row) > 16 and row[16] else ""
    else:
        try:
            intensity = int(row.get("Emotional Intensity", "").strip() or 0)
        except Exception:
            intensity = 0
        try:
            faith_score = int(row.get("Faith Score", "").strip() or 0)
        except Exception:
            faith_score = 0
        spillover_flag = row.get("Spillover Flag", "").strip()
        try:
            em_int = int(row.get("Emotional Intensity", "").strip() or 0)
        except Exception:
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
        "metadata": metadata
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

    # Per-tenant entity mappings — read once per run (M6): a tenant's
    # config edits apply on the next run; no per-row DB round-trip.
    mappings = get_entity_mappings()

    rows = fetch_sheet_data()
    print(f"Fetched {len(rows)} rows from Google Sheet")
    
    inserted = 0
    skipped = 0
    
    for row in rows:
        parsed = process_row(row)
        
        if not parsed["created_at"]:
            print("Skipping row with no valid timestamp")
            continue
        
        if last_sync and parsed["created_at"] <= last_sync:
            skipped += 1
            continue
        
        if check_duplicate(parsed["created_at"], parsed["content"]):
            skipped += 1
            continue
        
        if not parsed["content"].strip():
            skipped += 1
            continue
        
        embedding = get_embedding_sync(parsed["content"])
        
        try:
            result = supabase.table("memories").insert({
                "created_at": parsed["created_at"],
                "content": parsed["content"],
                "memory_type": "archive",
                "metadata": parsed["metadata"],
                "embedding": embedding if embedding else None
            }).execute()
            
            memory_id = result.data[0]["id"] if result.data else None
            
            if memory_id:
                if not embedding:
                    print("Skipping graphify for row — embedding failed")
                else:
                    graphify(parsed["content"], memory_id, mappings)
                schedule_index_memory(memory_id, parsed["content"], "archive", "archive_ingest")
            
            inserted += 1
            if inserted % 10 == 0:
                print(f"Inserted {inserted} memories...")
                
        except Exception as e:
            print(f"Error inserting row: {e}")
            continue
    
    print(f"\nComplete: {inserted} inserted, {skipped} skipped (incremental + duplicates)")


if __name__ == "__main__":
    with channel_tenant_scope():
        run_ingest()