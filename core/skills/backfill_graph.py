import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import uuid
import httpx

from core.lib.rate_limiter import flash_lite_limiter
from core.lib.people_utils import normalize_person_name, is_blocklisted_person
from core.lib.audit_logger import audit_log_sync
from core.lib.graph_rules import validate_edge
from core.services.db import get_supabase
from core.services.pipeline_service import add_to_failed_queue
from core.services.llm import get_gemini_client

supabase = get_supabase()
gemini_client = get_gemini_client()

# OpenRouter config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")
PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")
GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit', '429']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']


def call_llm_with_fallback_sync(
    prompt: str,
    model: str = None,
    config: dict = None,
    is_critical: bool = True,
    require_json: bool = False
):
    """
    Synchronous multi-provider LLM call with fallback chain.
    Provider chain:
    1. Primary: Gemini (gemini-3.1-flash-lite)
    2. Fallback: Gemma (gemma-4-31b-it)
    3. Fallback: OpenRouter (nvidia/nemotron-3-super-120b-a12b:free)
    """
    if model is None:
        model = "gemini-3.1-flash-lite"
    
    max_retries_per_provider = 2 if is_critical else 1
    base_delay = 8 if is_critical else 4
    
    def _call_gemini(p, cfg):
        return gemini_client.models.generate_content(
            model=model,
            contents=p,
            config=cfg or {}
        )
    
    def _call_gemma(p, cfg):
        return gemini_client.models.generate_content(
            model=GEMMA_FALLBACK_MODEL,
            contents=p,
            config=cfg or {}
        )
    
    def _call_openrouter(p, cfg):
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": PULSE_HTTP_REFERER,
            "X-Title": PULSE_APP_NAME
        }
        system_instruction = cfg.get('system_instruction') if cfg else None
        temperature = cfg.get('temperature', 0.7) if cfg else 0.7
        
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        
        body = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": temperature
        }
        
        if cfg and cfg.get('response_mime_type') == "application/json":
            body["response_format"] = {"type": "json_object"}
        
        resp = httpx.post(OPENROUTER_BASE_URL, json=body, headers=headers, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        
        class SimpleResponse:
            def __init__(self, text):
                self.text = text
        
        if 'choices' in data and len(data['choices']) > 0:
            return SimpleResponse(data['choices'][0]['message']['content'])
        return SimpleResponse(data.get('content', '') or json.dumps(data))
    
    providers = [
        {"provider": "gemini", "model": model, "fn": _call_gemini},
        {"provider": "gemma", "model": GEMMA_FALLBACK_MODEL, "fn": _call_gemma},
    ]
    
    if OPENROUTER_API_KEY:
        providers.append({
            "provider": "openrouter",
            "model": OPENROUTER_MODEL,
            "fn": _call_openrouter
        })
    
    last_error = None
    
    for provider_idx, prov in enumerate(providers):
        provider_name = prov["provider"]
        model_name = prov["model"]
        
        for attempt in range(max_retries_per_provider):
            try:
                # Rate limit: only for Gemini flash-lite model
                if provider_name == "gemini" and "flash-lite" in model_name:
                    flash_lite_limiter.acquire()
                response = prov["fn"](prompt, config)
                
                if hasattr(response, 'text'):
                    response_text = response.text
                else:
                    response_text = str(response)
                
                if require_json:
                    try:
                        if response_text.strip().startswith('{') or response_text.strip().startswith('['):
                            json.loads(response_text)
                    except ValueError as pe:
                        audit_log_sync("backfill_graph", "WARNING", f"⚠️ LLM JSON parse failed provider={provider_name}: {pe}")
                        if provider_idx == len(providers) - 1:
                            raise
                        continue
                
                print(f"✓ LLM success provider={provider_name} model={model_name}")
                return response
                
            except Exception as e:
                error_str = str(e).lower()
                
                is_retryable = any(err in error_str for err in RETRYABLE_ERRORS)
                is_non_retryable = any(err in error_str for err in NON_RETRYABLE_ERRORS)
                
                if is_non_retryable:
                    audit_log_sync("backfill_graph", "ERROR", f"✗ LLM non-retryable error provider={provider_name}: {e}")
                    raise
                
                if is_retryable and attempt < max_retries_per_provider - 1:
                    delay = base_delay * (2 ** attempt)
                    audit_log_sync("backfill_graph", "WARNING", f"⚠️ LLM retry provider={provider_name} attempt={attempt+1} delay={delay:.0f}s error={error_str[:50]}")
                    time.sleep(delay)
                    continue
                
                audit_log_sync("backfill_graph", "WARNING", f"⚠️ LLM provider failed provider={provider_name} model={model_name}: {error_str[:80]}")
                last_error = e
                break
        
        if provider_idx < len(providers) - 1:
            print(f"🔄 LLM fallback -> {providers[provider_idx + 1]['provider']}")
    
    raise last_error or Exception("All LLM providers failed")

BATCH_SIZE = 50  # Process more memories per batch
MEMORY_TYPES = [
    "Journal", "note", "outcome", "reflection", "relationship_note"
]


def with_retry(fn, retries=3, base_delay=1, label="operation"):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                wait = base_delay * (2 ** attempt)
                audit_log_sync("backfill_graph", "ERROR", f"{label} failed (attempt {attempt+1}/3), retrying in {wait}s... Error: {e}")
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
            meta = _normalize_meta(row.get("metadata"))
            if meta.get("memory_id"):
                # Normalize: treat as int for comparison with memories.id
                try:
                    processed_memory_ids.add(int(meta["memory_id"]))
                except (ValueError, TypeError) as e:
                    audit_log_sync("backfill_graph", "WARNING", f"⚠️ memory_id parse error: {e}")
        except Exception as e:
            audit_log_sync("backfill_graph", "WARNING", f"⚠️ Metadata processing error: {e}")
    
    total_memories = fetch_all_paginated("memories", "id, memory_type, created_at")
    print("  MEMORY DIAGNOSTICS:")
    print(f"    Total memories in DB: {len(total_memories) if total_memories else 0}")
    
    memories = fetch_all_paginated("memories", "id, content, memory_type, metadata, created_at", "memory_type", MEMORY_TYPES)
    
    # URL FILTER: Strip out any memory that contains a URL
    filtered_memories = [m for m in (memories or []) if 'http://' not in str(m.get('content', '')).lower() and 'https://' not in str(m.get('content', '')).lower()]
    
    print(f"    Memories matching MEMORY_TYPES filter: {len(memories) if memories else 0}")
    print(f"    Memories after URL filtering: {len(filtered_memories)}")
    
    # Count by type
    if filtered_memories:
        type_counts = {}
        for m in filtered_memories:
            t = m.get("memory_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"      {t}: {c}")
    
    # Filter to only unprocessed memories (fix int/string type mismatch)
    final_memories = [m for m in filtered_memories if m["id"] not in processed_memory_ids]
    print(f"    Already in graph edges (skipped): {len(processed_memory_ids)}")
    print(f"    New memories to process: {len(final_memories)}")
    
    return final_memories
    

pending_entities_cache = set()

def fetch_pending_entities():
    global pending_entities_cache
    try:
        res = fetch_all_paginated("pending_graph_nodes", "label", in_filter_col="status", in_filter_val=["pending", "approved", "rejected"])
        pending_entities_cache = {n['label'] for n in res}
    except Exception:
        pass

def _check_pending_label_exists(label: str) -> bool:
    label_clean = label.strip().lower()
    existing = supabase.table("pending_graph_nodes") \
        .select("id") \
        .filter("label", "ilike", label_clean) \
        .limit(1) \
        .execute()
    if existing.data:
        return True
    if len(label_clean) >= 6:
        existing = supabase.table("pending_graph_nodes") \
            .select("id") \
            .filter("label", "ilike", f"%{label_clean}%") \
            .limit(1) \
            .execute()
        if existing.data:
            return True
    return False

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
    metadata = _normalize_meta(memory.get("metadata"))
    
    if memory_type == "Prophecy":
        entry_type = metadata.get("entry_type", "")
        return f"[PROPHECY:{entry_type}] {content}" if entry_type else content
    
    elif memory_type in ["Psalm", "Prayer"]:
        tags = metadata.get("tags", "")
        if tags:
            return f"[TAGS:{tags}] {content}"
        return content
    
    return content



# ── EMBEDDING BACKFILL ──────────────────────────────────────────────────────

def get_embedding(text: str) -> list | None:
    """Generate a 768-dim embedding via Gemini for a given text string."""
    if not text or not text.strip():
        return None
    try:
        result = with_retry(
            lambda: gemini_client.models.embed_content(
                model="gemini-embedding-2-preview",
                contents=text,
                config={"task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 768}
            ),
            retries=3,
            base_delay=2,
            label="Gemini embedding"
        )
        if result and result.embeddings:
            return result.embeddings[0].values
        return None
    except Exception as e:
        print(f"Embedding failed: {e}")
        return None


def backfill_embeddings():
    """
    Finds all rows in `memories` where embedding IS NULL,
    generates embeddings via Gemini, and patches them back.
    """
    print("\n🔍 Embedding backfill: fetching memories with missing embeddings...")

    all_rows = []
    start = 0
    page_size = 500

    while True:
        try:
            res = with_retry(
                lambda: supabase.table("memories")
                    .select("id, content, memory_type, metadata")
                    .in_("memory_type", MEMORY_TYPES)
                    .is_("embedding", "null")
                    .range(start, start + page_size - 1)
                    .execute(),
                label="Fetch missing embeddings"
            )
            data = res.data or []
        except Exception as e:
            print(f"Failed to fetch missing-embedding rows: {e}")
            break

        all_rows.extend(data)
        if len(data) < page_size:
            break
        start += page_size

    total = len(all_rows)
    print(f"Found {total} memories with missing embeddings.\n")

    if total == 0:
        print("✅ No missing embeddings — all caught up!")
        return

    success = 0
    failed = 0

    for i, row in enumerate(all_rows):
        memory_id = row["id"]
        content = synthesize_content(row)

        if not content.strip():
            print(f"  [{i+1}/{total}] Skipping {memory_id} — empty content.")
            failed += 1
            continue

        embedding = get_embedding(content)

        if not embedding:
            audit_log_sync("backfill_graph", "ERROR", f"  [{i+1}/{total}] ❌ Embedding failed for {memory_id}")
            failed += 1
            continue

        try:
            with_retry(
                lambda: supabase.table("memories")
                    .update({"embedding": embedding, "embedding_status": "success"})
                    .eq("id", memory_id)
                    .execute(),
                label=f"Update embedding for {memory_id}"
            )
            print(f"  [{i+1}/{total}] ✅ Patched embedding for {memory_id} ({row['memory_type']})")
            success += 1
        except Exception as e:
            # Mark as failed
            try:
                supabase.table("memories").update({"embedding_status": "failed"}).eq("id", memory_id).execute()
            except Exception:
                pass
            
            # Add to failed queue for retry (use sync version)
            try:
                # Since backfill_graph.py is sync, we need to handle the async function
                import asyncio
                if asyncio.get_event_loop().is_running():
                    asyncio.create_task(
                        add_to_failed_queue("memories", str(memory_id), "embedding_backfill", str(e))
                    )
                else:
                    # Run in new event loop
                    asyncio.run(
                        add_to_failed_queue("memories", str(memory_id), "embedding_backfill", str(e))
                    )
            except Exception as qe:
                audit_log_sync("backfill_graph", "WARNING", f"Failed to add to queue: {qe}")
            
            audit_log_sync("backfill_graph", "ERROR", f"  [{i+1}/{total}] ❌ DB update failed for {memory_id}: {e}")
            failed += 1

        # Small delay to stay within Gemini rate limits
        time.sleep(0.3)

    audit_log_sync("backfill_graph", "ERROR", f"\n🏁 Embedding backfill complete! ✅ Success: {success}  ❌ Failed: {failed}")

# ── END EMBEDDING BACKFILL ──────────────────────────────────────────────────


def extract_graph_elements(text: str, memory_id: str, known_entities: set = None) -> dict:
    known_entities = known_entities or set()
    # Pre-process: strip URLs and resource/cluster fragments to prevent extracting entities from them
    import re
    cleaned_text = re.sub(r'\[RESOURCE\].*?(\n|$)', '', text, flags=re.IGNORECASE)
    cleaned_text = re.sub(r'\[CLUSTER\].*?(\n|$)', '', cleaned_text, flags=re.IGNORECASE)
    cleaned_text = re.sub(r'https?://\S+', '', cleaned_text)
    
    known_list = ", ".join(sorted(known_entities)) if known_entities else "None"
    prompt = f"""Extract knowledge graph elements from this text.
    
Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "person"|"organization"|"project"|"place"|"animal"}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string}}
    
Text: {cleaned_text}
    
Rules:
- Extract People (names), Organizations, Projects, Places, and Animals as nodes
- Create edges for relationships between nodes
- Use UPPERCASE relationship types: "DISCUSSED_WITH", "WORKS_AT", "WORKS_ON", "CLIENT_OF", "VENDOR_TO", "MEMBER_OF", "PARENT_OF", "SPOUSE_OF", "SIBLING_OF", "FAMILY_OF", "PET_OF", "FRIEND_OF", "MET_WITH", "INTRODUCED", "MENTORS", "SERVES_AT"
- PROJECT DEFINITION: A named initiative with a defined goal and stakeholders.
  ✓ QHORD, Ashraya, Solvstrat, Rhodey OS
  ✗ "Church cash rotation incident" (event), "New Habit" (intention), "Journaling tool" (concept), "Call Marcus" (task)
  If it doesn't have a formal name someone would use to refer to an ongoing initiative — skip it.
- CRITICAL RULE: EVERY node you extract MUST have at least one connecting edge. Do not output isolated nodes.
- CRITICAL RULE: Only extract entities that are explicitly, verbatim stated in the text. Do NOT infer, guess, or add external knowledge.
- Standardize labels to Title Case.
- CRITICAL: Do NOT extract anything from URLs, file paths, or online handles except to tag them as resources.
- CONSISTENCY: EVERY label referenced in an edge's "source" or "target" MUST also appear in the "nodes" array with its type.
- Existing approved entities (person, org, project): {known_list}
- Do NOT create nodes for entities not in this list unless they are a clearly identifiable place or animal."""
    
    try:
        response = call_llm_with_fallback_sync(
            prompt=prompt,
            model="gemini-3.1-flash-lite",
            config={"response_mime_type": "application/json"},
            is_critical=False,
            require_json=True
        )
        
        if hasattr(response, 'text') and response.text:
            result = json.loads(response.text)
            if isinstance(result, dict) and ('nodes' in result or 'edges' in result):
                # Guard B: Text-anchoring validation
                text_lower = text.lower()
                valid_nodes = []
                for n in result.get('nodes', []):
                    label = n.get('label', '')
                    if label.lower() in text_lower or label.lower() == 'danny':  # Danny is always valid for AUTHORED edges
                        valid_nodes.append(n)
                    else:
                        audit_log_sync("backfill_graph", "WARNING", f"    ⚠️ Dropped hallucinated node: {label}")
                
                valid_labels = {n.get('label', '').lower() for n in valid_nodes}
                valid_edges = []
                for e in result.get('edges', []):
                    if e.get('source', '').lower() in valid_labels and e.get('target', '').lower() in valid_labels:
                        valid_edges.append(e)
                
                result['nodes'] = valid_nodes
                result['edges'] = valid_edges

                print(f"    Extracted {len(valid_nodes)} valid nodes, {len(valid_edges)} valid edges from memory {memory_id}")
                return result
            else:
                audit_log_sync("backfill_graph", "WARNING", f"    ⚠️ Invalid response format from memory {memory_id}: {str(result)[:100]}")
                return {"nodes": [], "edges": []}
        else:
            audit_log_sync("backfill_graph", "WARNING", f"    ⚠️ Empty response for memory {memory_id}")
            return {"nodes": [], "edges": []}
    except Exception as e:
        audit_log_sync("backfill_graph", "ERROR", f"    ❌ Graph extraction failed for memory {memory_id}: {e}")
        return {"nodes": [], "edges": []}

def is_real_project(label: str) -> bool:
    try:
        result = supabase.table('projects').select('id').ilike('name', label.strip()).execute()
        return len(result.data) > 0
    except Exception:
        return False

GROUNDED_TYPES = {
    'project': ('projects', 'name'),
    'person': ('people', 'name'),
    'organization': ('organizations', 'name'),
}

def has_structural_anchor(label: str, node_type: str) -> bool:
    if node_type not in GROUNDED_TYPES or GROUNDED_TYPES[node_type] is None:
        return True  # no check available — allow through
    table, column = GROUNDED_TYPES[node_type]
    try:
        result = supabase.table(table).select('id').ilike(column, label.strip()).execute()
        return len(result.data) > 0
    except Exception:
        return True

def get_or_create_node(label: str, node_type: str, graph_entities: dict, created_nodes: dict, memory_id: str = None) -> str:
    """
    Get or create a graph node with proper type handling.
    If node exists, updates its type to match the latest extracted type.
    """
    if label in created_nodes:
        return created_nodes[label]
        
    # PHASE 2 HOOK
    from core.clarifier import evaluate_node
    evaluate_node({"label": label, "type": node_type})
    
    # GUARD 2: Entity Grounding for projects
    if node_type == 'project' and not is_real_project(label):
        audit_log_sync("backfill_graph", "WARNING", f"Skipped ungrounded project node: {label}")
        return None

    # Check if already in graph_entities (DB cache)
    if label in graph_entities:
        node_id = graph_entities[label]["id"]
        # Update type if different
        existing_type = graph_entities[label].get("type", "concept")
        if existing_type != node_type:
            try:
                supabase.table("graph_nodes").update({"type": node_type}).eq("id", node_id).execute()
                graph_entities[label]["type"] = node_type
                audit_log_sync("backfill_graph", "INFO", 
                    f"Updated node '{label}' type: {existing_type} → {node_type}")
            except Exception as e:
                audit_log_sync("backfill_graph", "WARNING", f"Node type update failed for '{label}': {e}")
        created_nodes[label] = node_id
        return node_id
    
    # Node doesn't exist - create it
    if node_type in ['person', 'project', 'organization']:
        if label not in pending_entities_cache and not _check_pending_label_exists(label):
            try:
                status = "pending" if has_structural_anchor(label, node_type) else "flagged"
                supabase.table("pending_graph_nodes").insert({
                    "label": label,
                    "type": node_type,
                    "source_text": memory_id,
                    "status": status
                }).execute()
                pending_entities_cache.add(label)
                audit_log_sync("backfill_graph", "INFO", f"Queued new entity for approval ({status}): {label} ({node_type})")
            except Exception as e:
                audit_log_sync("backfill_graph", "ERROR", f"Pending node insert error: {e}")
        elif label not in pending_entities_cache:
            pending_entities_cache.add(label)
        return None

    try:
        result = supabase.table("graph_nodes").upsert(
            {"label": label, "type": node_type, "metadata": {"source": "backfill_graph", "memory_id": memory_id}},
            on_conflict="label"
        ).execute()
        
        if result.data:
            node_id = result.data[0]["id"]
        else:
            # Fetch the created/updated node
            res = supabase.table("graph_nodes") \
                .select("id") \
                .eq("label", label) \
                .single() \
                .execute()
            node_id = res.data["id"] if res.data else None
    except Exception as e:
        audit_log_sync("backfill_graph", "WARNING", f"Node upsert failed for '{label}': {e}")
        return None
    
    if node_id:
        created_nodes[label] = node_id
        graph_entities[label] = {"id": node_id, "type": node_type}
    return node_id

def upsert_nodes(nodes: list, graph_entities: dict, memory_id: str):
    if not nodes:
        return
    
    node_records = []
    for node in nodes:
        label = node.get("label", "")
        node_type = node.get("type", "concept")
        
        # PHASE 2 HOOK
        from core.clarifier import evaluate_node
        evaluate_node(node)
        
        existing = graph_entities.get(label, {})
        existing_id = existing.get("id")
        existing_type = existing.get("type", "concept")
        
        record = {
            "label": label,
            "type": node_type,  # Always use latest extracted type
            "metadata": {"source": "backfill_graph", "memory_id": memory_id}
        }
        
        if existing_id:
            record["id"] = existing_id
            # If type changed, update it
            if existing_type != node_type:
                record["type"] = node_type
            node_records.append(record)
        else:
            if node_type == 'project' and not is_real_project(label):
                audit_log_sync("backfill_graph", "WARNING", f"Skipped ungrounded project node: {label}")
                continue

            if node_type in ['person', 'project', 'organization']:
                # Gated high-risk entity - send to pending
                if label not in pending_entities_cache and not _check_pending_label_exists(label):
                    try:
                        status = "pending" if has_structural_anchor(label, node_type) else "flagged"
                        supabase.table("pending_graph_nodes").insert({
                            "label": label,
                            "type": node_type,
                            "source_text": memory_id,
                            "status": status
                        }).execute()
                        pending_entities_cache.add(label)
                        audit_log_sync("backfill_graph", "INFO", f"Queued new high-risk entity for approval ({status}): {label} ({node_type})")
                    except Exception as e:
                        audit_log_sync("backfill_graph", "ERROR", f"Pending node insert error: {e}")
                elif label not in pending_entities_cache:
                    pending_entities_cache.add(label)
            else:
                record["id"] = str(uuid.uuid4())
                node_records.append(record)
    
    if node_records:
        try:
            supabase.table("graph_nodes").upsert(
                node_records,
                on_conflict="label"
            ).execute()
            # Update local cache
            for node in node_records:
                graph_entities[node["label"]] = {"id": node["id"], "type": node["type"]}
        except Exception as e:
            audit_log_sync("backfill_graph", "ERROR", f"Node upsert error: {e}")


def _build_label_type_cache() -> dict:
    result = supabase.table("graph_nodes").select("label, type").execute()
    cache = {}
    for n in result.data or []:
        cache[n["label"].lower().strip()] = n["type"]
    return cache


def insert_pending_edges_batch(edges: list):
    """Insert edges into pending_graph_edges in batches, ignoring duplicates.
    Runs Phase 3 validation: auto-rejects banned relationships, auto-corrects INVALID_COMBOS."""
    if not edges:
        return
    try:
        label_type_cache = _build_label_type_cache()
        existing_res = supabase.table("pending_graph_edges").select("source_label,target_label,relationship").eq("status", "pending").execute()
        existing_set = {f"{r['source_label']}|{r['target_label']}|{r['relationship']}" for r in (existing_res.data or [])}

        to_insert = []
        for edge in edges:
            s_label = edge.get("source_label", "")
            t_label = edge.get("target_label", "")
            rel = edge.get("relationship", "").upper()
            s_type = label_type_cache.get(s_label.lower().strip())
            t_type = label_type_cache.get(t_label.lower().strip())
            if s_type and t_type:
                vr = validate_edge(s_type, rel, t_type)
                if vr["action"] == "auto_reject":
                    audit_log_sync("backfill_graph", "INFO", f"Auto-rejected {s_label} --[{rel}]--> {t_label}: {vr['reason']}")
                    continue
                elif vr["action"] == "auto_correct":
                    edge["relationship"] = vr["reason"]
                    rel = vr["reason"]
                    audit_log_sync("backfill_graph", "INFO", f"Auto-corrected {s_label} --[{rel}]--> {t_label}")
            key = f"{s_label}|{t_label}|{rel}"
            if key not in existing_set:
                to_insert.append(edge)
                existing_set.add(key)

        if not to_insert:
            return

        for i in range(0, len(to_insert), 100):
            batch = to_insert[i:i+100]
            supabase.table("pending_graph_edges").insert(batch).execute()
    except Exception as e:
        audit_log_sync("backfill_graph", "ERROR", f"Pending edge insert failed: {e}")

def insert_edges(edges: list, node_label_to_id: dict, memory_id: str, source_table: str = "memories"):
    """Queue extracted edges for human approval in pending_graph_edges."""
    pending_batch = []
    for edge in edges:
        source_label = edge.get("source", "")
        target_label = edge.get("target", "")
        relationship = edge.get("relationship", "relates_to").upper()
        
        if not source_label or not target_label:
            continue
            
        pending_batch.append({
            "source_label": source_label,
            "target_label": target_label,
            "relationship": relationship,
            "source_text": f"{source_table}:{memory_id}",
            "source_table": source_table,
            "status": "pending"
        })
        
    insert_pending_edges_batch(pending_batch)


def process_memory(memory: dict, graph_entities: dict, source_table: str = "memories") -> bool:
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
        node_type = node.get("type", "concept")
        node_id = get_or_create_node(label, node_type, graph_entities, created_nodes, memory_id)
        if node_id:
            node_label_to_id[label] = node_id
    
    if not node_label_to_id:
        danny_id = get_or_create_node("Danny", "person", graph_entities, created_nodes, memory_id)
        if danny_id:
            node_label_to_id["Danny"] = danny_id
    
    insert_edges(edges, node_label_to_id, memory_id, source_table)
    
    return True


def cleanup_resource_edges():
    """
    One-time/routine cleanup: Rejects pending edges derived from memories containing resource/cluster content.
    """
    print("\n🧹 Cleaning up pending edges derived from resource/cluster content...")
    try:
        # Find memories containing [RESOURCE] or URLs
        res1 = supabase.table('memories').select('id').eq('memory_type', 'canonical_page').ilike('content', '%[RESOURCE]%').execute()
        res2 = supabase.table('memories').select('id').ilike('content', '%http%').execute()
        
        mem_ids_1 = [str(m['id']) for m in (res1.data or [])]
        mem_ids_2 = [str(m['id']) for m in (res2.data or [])]
        memory_ids = list(set(mem_ids_1 + mem_ids_2))
        
        if not memory_ids:
            print("  No URL/resource-contaminated memories found.")
            return
            
        print(f"  Found {len(memory_ids)} memories containing resources or URLs.")
        
        # Also clean up any edges from raw_dumps with URLs if needed, but focus on resources for now
        rejected_count = 0
        for i in range(0, len(memory_ids), 50):
            batch = memory_ids[i:i+50]
            update_res = supabase.table('pending_graph_edges') \
                .update({"status": "rejected"}) \
                .in_('source_text', batch) \
                .eq('status', 'pending') \
                .execute()
            rejected_count += len(update_res.data or [])
            
        print(f"  ✅ Rejected {rejected_count} pending edges that came from resources.")
    except Exception as e:
        audit_log_sync("backfill_graph", "ERROR", f"Cleanup resource edges failed: {e}")

def run_backfill():
    # ── Step 1: Patch missing embeddings first ──────────────────────────────
    backfill_embeddings()

    # ── Step 2: Backfill graph edges ────────────────────────────────────────
    print("\n🔗 Graph backfill: fetching memories for graph edges...")
    memories = fetch_memories()
    print(f"Found {len(memories)} memories to process for graph edges.")
    
    print("Building graph entities lookup...")
    graph_entities = fetch_graph_entities()
    print(f"Found {len(graph_entities)} entities (people + projects)")
    
    fetch_pending_entities()
    
    processed = 0
    failed = 0
    
    for i in range(0, len(memories), BATCH_SIZE):
        batch = memories[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"Processing batch {batch_num} ({len(batch)} memories)...")
        
        extracted_data = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_mem = {
                executor.submit(
                    extract_graph_elements, synthesize_content(m), m["id"], fetch_known_entities()
                ): m for m in batch if synthesize_content(m).strip()
            }
            
            for future in as_completed(future_to_mem):
                mem = future_to_mem[future]
                try:
                    graph_data = future.result()
                    nodes = graph_data.get("nodes", [])
                    edges = graph_data.get("edges", [])
                    if nodes or edges:
                        extracted_data.append({"memory_id": mem["id"], "source_table": mem.get("_source_table", "memories"), "nodes": nodes, "edges": edges})
                        processed += 1
                    else:
                        failed += 1
                except Exception as e:
                    audit_log_sync("backfill_graph", "ERROR", f"Error processing memory {mem['id']}: {e}")
                    failed += 1
        
        if not extracted_data:
            continue
            
        all_nodes = []
        all_edges = []
        for data in extracted_data:
            all_nodes.extend(data["nodes"])
            for edge in data["edges"]:
                all_edges.append({
                    "source": edge.get("source", ""),
                    "target": edge.get("target", ""),
                    "relationship": edge.get("relationship", "relates_to").upper(),
                    "memory_id": data["memory_id"], "source_table": data["source_table"]
                })
                
        unique_nodes = {}
        for node in all_nodes:
            label = node.get("label", "")
            if not label:
                continue
            unique_nodes[label] = node.get("type", "concept")
            
        for edge in all_edges:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            if src and src not in unique_nodes:
                unique_nodes[src] = "concept"
            if tgt and tgt not in unique_nodes:
                unique_nodes[tgt] = "concept"
            
        if "Danny" not in unique_nodes:
            unique_nodes["Danny"] = "person"
            
        # Batch upsert nodes using the existing upsert_nodes function
        upsert_nodes([{"label": k, "type": v} for k, v in unique_nodes.items()], graph_entities, "batch")
        
        pending_edges_to_insert = []
        for edge in all_edges:
            pending_edges_to_insert.append({
                "source_label": edge["source"],
                "target_label": edge["target"],
                "relationship": edge["relationship"],
                "source_text": f"{edge['source_table']}:{edge['memory_id']}",
                "source_table": edge['source_table'],
                "status": "pending"
            })
                
        if pending_edges_to_insert:
            insert_pending_edges_batch(pending_edges_to_insert)
                
        print(f"Completed batch {batch_num}")
    
    print(f"Graph backfill complete! Processed: {processed}, Skipped: {failed}")

    # Cleanup step: reject any resource/cluster derived pending edges
    cleanup_resource_edges()

    # Tier 1: Backfill orphaned tasks
    backfill_orphaned_tasks()
    
    # Tier 1.5: Backfill emotion edges
    backfill_emotion_edges()
    backfill_orphaned_node_edges()

    # Notify on failure via Telegram
    if failed > 0:
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if telegram_chat_id and telegram_bot_token:
            try:
                import httpx
                message = f"⚠️ Graph Backfill: {failed} items failed. Check GitHub Actions logs."
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                payload = {"chat_id": int(telegram_chat_id), "text": message, "parse_mode": "Markdown"}
                httpx.post(url, json=payload, timeout=10)
            except Exception as e:
                print(f"Telegram notify failed: {e}")

def backfill_emotion_edges():
    """
    Tier 1.5: Backfills Danny -> FEELS -> emotional_state edges.
    Runs every cycle to prevent orphaned emotion nodes.
    """
    print("\n❤️ Emotion backfill: Fixing orphaned emotional states...")
    try:
        # Step A: Reclassify emotional concepts
        # 1. Exact match for high-severity nodes
        supabase.table("graph_nodes").update({"type": "emotional_state"}).eq("type", "concept").in_(
            "label", ['Suicidal Ideation', 'Suicidal', 'Depression', 'Broken', 'Desperate', 'Anxiety', 'anxiety']
        ).execute()

        # 2. ILIKE match for broader catch
        emotional_patterns = [
            '%suicidal%', '%depression%', '%hopeless%', '%helpless%',
            '%loneliness%', '%lonely%', '%desperate%', '%frustrated%',
            '%regret%', '%guilt%', '%crushed%', '%betrayed%', '%pain%',
            '%nervous%', '%worried%', '%angry%', '%afraid%', '%ashamed%',
            '%stressed%', '%overwhelmed%', '%exhausted%', '%tired%',
            '%grief%', '%fear%', '%confused%', '%lost%'
        ]
        
        for pattern in emotional_patterns:
            supabase.table("graph_nodes").update({"type": "emotional_state"}).eq("type", "concept").ilike("label", pattern).execute()
        
        # Step B: Create Danny -> FEELS edges
        # We need to do this via Postgres function or multiple calls since Supabase REST API doesn't support complex cross-joins.
        # Alternatively, we can fetch all emotional_state nodes, fetch Danny's ID, and insert edges.
        
        danny_res = supabase.table("graph_nodes").select("id").eq("label", "Danny").eq("type", "person").maybe_single().execute()
        if not danny_res or not danny_res.data:
            print("Danny node not found, skipping emotion edge backfill.")
            return
        
        danny_id = danny_res.data["id"]
        
        es_nodes = fetch_all_paginated("graph_nodes", "id, label", in_filter_col="type", in_filter_val=["emotional_state"])
        if not es_nodes:
            return
            
        feels_edges = []
        page = 0
        limit = 1000
        while True:
            res = supabase.table("graph_edges").select("target_node_id").eq("source_node_id", danny_id).eq("relationship", "FEELS").range(page*limit, (page+1)*limit - 1).execute()
            data = res.data or []
            feels_edges.extend([e["target_node_id"] for e in data])
            if len(data) < limit:
                break
            page += 1
            
        existing_target_ids = set(feels_edges)
        
        edges_to_insert = []
        for es in es_nodes:
            if es["id"] not in existing_target_ids:
                edges_to_insert.append({
                    "source_label": "Danny",
                    "target_label": es["label"],
                    "relationship": "FEELS",
                    "source_text": "backfill_emotions",
                    "status": "pending"
                })
        
        if edges_to_insert:
            print(f"Queueing {len(edges_to_insert)} pending FEELS edges...")
            insert_pending_edges_batch(edges_to_insert)
        
        print("✅ Emotion backfill complete.")
    except Exception as e:
        audit_log_sync("backfill_graph", "ERROR", f"Emotion backfill failed: {e}")

def backfill_orphaned_tasks():
    """Backfills graph nodes + edges for tasks with no corresponding graph_nodes entry."""
    print("\n🔄 Task backfill: Checking for orphaned tasks...")
    
    all_tasks = fetch_all_paginated("tasks", "id, title, project_id, status")
    if not all_tasks:
        print("No tasks found.")
        return
    
    existing_task_nodes = fetch_all_paginated("graph_nodes", "id, metadata", in_filter_col="type", in_filter_val=["task"])
    task_node_task_ids = set()
    for node in (existing_task_nodes or []):
        meta = _normalize_meta(node.get("metadata"))
        tid = meta.get("task_id")
        if tid:
            task_node_task_ids.add(int(tid))
    
    # Find existing task nodes that have ZERO edges
    all_edges = fetch_all_paginated("graph_edges", "source_node_id, target_node_id")
    task_nodes_with_edges = set()
    for e in (all_edges or []):
        task_nodes_with_edges.add(e["source_node_id"])
        task_nodes_with_edges.add(e["target_node_id"])
        
    edgeless_existing_tasks = []
    for node in (existing_task_nodes or []):
        if node["id"] not in task_nodes_with_edges:
            meta = _normalize_meta(node.get("metadata"))
            tid = meta.get("task_id")
            if tid:
                # Find original task data
                t_data = next((t for t in all_tasks if t["id"] == int(tid)), None)
                if t_data:
                    edgeless_existing_tasks.append(t_data)

    orphaned_tasks = [t for t in all_tasks if t["id"] not in task_node_task_ids]
    
    # Combine both completely missing tasks AND existing edgeless tasks
    combined_tasks_to_process = {t["id"]: t for t in orphaned_tasks + edgeless_existing_tasks}.values()
    orphaned_tasks = list(combined_tasks_to_process)

    print(f"Found {len(orphaned_tasks)} orphaned tasks (no graph node).")
    
    if not orphaned_tasks:
        return
    
    all_people = fetch_all_paginated("people", "id, name")
    all_projects = fetch_all_paginated("projects", "id, name")
    
    project_id_to_name = {p["id"]: p["name"] for p in all_projects}
    person_id_to_name = {p["id"]: p["name"] for p in all_people}
    
    count = 0
    for task in orphaned_tasks:
        task_id = task["id"]
        task_title = task.get("title", "Untitled")
        project_id = task.get("project_id")
        
        meta = {}
        if project_id:
            meta["project_id"] = project_id
        
        try:
            supabase.table("graph_nodes").upsert({
                "label": task_title,
                "type": "task",
                "metadata": {"source": "tasks_table", "task_id": task_id, **meta}
            }, on_conflict="label").execute()
            node_res = supabase.table("graph_nodes").select("id").eq("label", task_title).maybe_single().execute()
            if not node_res or not node_res.data:
                audit_log_sync("backfill_graph", "WARNING", f"⚠️ Failed to get node for task {task_id}")
                continue
            task_node_id = node_res.data["id"]
        except Exception as e:
            audit_log_sync("backfill_graph", "WARNING", f"⚠️ Failed to create node for task {task_id}: {e}")
            continue
        
        if project_id:
            proj_node = None
            # Try metadata->>legacy_id first (new style)
            try:
                proj_node = supabase.table("graph_nodes") \
                    .select("id") \
                    .in_("type", ["project", "cluster", "organization"]) \
                    .filter("metadata->>legacy_id", "eq", str(project_id)) \
                    .limit(1).maybe_single() \
                    .execute()
            except Exception:
                pass
            # Try metadata->>project_id (old style)
            if proj_node is None or proj_node.data is None:
                try:
                    proj_node = supabase.table("graph_nodes") \
                        .select("id") \
                        .in_("type", ["project", "cluster", "organization"]) \
                        .filter("metadata->>project_id", "eq", str(project_id)) \
                        .limit(1).maybe_single() \
                        .execute()
                except Exception:
                    proj_node = None
            # Fallback: label-based match using project name
            if (proj_node is None or proj_node.data is None) and project_id in project_id_to_name:
                proj_name = project_id_to_name[project_id]
                try:
                    proj_node = supabase.table("graph_nodes") \
                        .select("id") \
                        .in_("type", ["project", "cluster", "organization"]) \
                        .ilike("label", proj_name) \
                        .limit(1).maybe_single() \
                        .execute()
                except Exception:
                    proj_node = None
            
            if proj_node is not None and proj_node.data is not None:
                proj_node_id = proj_node.data["id"]
                try:
                    try:
                        # Clean up any existing BELONGS_TO edges for this task to avoid orphans
                        supabase.table('graph_edges') \
                            .delete() \
                            .eq('relationship', 'BELONGS_TO') \
                            .filter('metadata->>task_id', 'eq', str(task_id)) \
                            .execute()
                    except Exception as clean_err:
                        audit_log_sync("backfill_graph", "WARNING", f"Failed to clean up stale BELONGS_TO edges: {clean_err}")

                    supabase.table("graph_edges").insert({
                        "source_node_id": task_node_id,
                        "target_node_id": proj_node_id,
                        "relationship": "BELONGS_TO",
                        "weight": 1.0,
                        "metadata": {"source": "task_engine", "task_id": task_id}
                    }).execute()
                except Exception as e:
                    audit_log_sync("backfill_graph", "WARNING", f"⚠️ BELONGS_TO edge failed for task {task_id}: {e}")
        
        search_text = task_title.lower()
        
        for pid, pname in person_id_to_name.items():
            if pname.lower() in search_text:
                # Try metadata->>people_id first (new style)
                person_node = None
                try:
                    person_node = supabase.table("graph_nodes") \
                        .select("id") \
                        .eq("type", "person") \
                        .filter("metadata->>people_id", "eq", str(pid)) \
                        .maybe_single() \
                        .execute()
                except Exception:
                    pass
                # Fallback: label-based match
                if person_node is None or person_node.data is None:
                    try:
                        person_node = supabase.table("graph_nodes") \
                            .select("id") \
                            .eq("type", "person") \
                            .ilike("label", pname) \
                            .maybe_single() \
                            .execute()
                    except Exception:
                        person_node = None
                
                if person_node and person_node.data:
                    person_node_id = person_node.data["id"]
                    try:
                        existing_edge = supabase.table("graph_edges") \
                            .select("id") \
                            .eq("source_node_id", task_node_id) \
                            .eq("target_node_id", person_node_id) \
                            .eq("relationship", "INVOLVES") \
                            .maybe_single() \
                            .execute()
                        
                        if not existing_edge or not existing_edge.data:
                            supabase.table("graph_edges").insert({
                                "source_node_id": task_node_id,
                                "target_node_id": person_node_id,
                                "relationship": "INVOLVES",
                                "weight": 1.0,
                                "metadata": {
                                    "source": "task_engine",
                                    "task_id": task_id,
                                    "matched_name": pname
                                }
                            }).execute()
                    except Exception as e:
                        audit_log_sync("backfill_graph", "WARNING", f"⚠️ INVOLVES edge failed for task {task_id}: {e}")
        
        count += 1
    
    print(f"✅ Task backfill complete: {count} tasks processed.")


def _normalize_meta(raw) -> dict:
    """Normalize graph node metadata to dict. Handles str, dict, and list JSONB values."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, list):
        return {}
    return {}



def backfill_orphaned_node_edges():
    """
    Tier 1.8: Re-wires isolated and semi-isolated nodes to Danny.
    Handles:
    1. Nodes with NO direct connection to Danny
    2. Nodes where the ONLY connection to Danny is 'AUTHORED' (upgrades it/adds semantic link)
    """
    print("\n🕸️  Node Edge Backfill: Checking for isolated/semi-isolated nodes...")
    
    # Get Danny's node ID
    danny_res = supabase.table("graph_nodes").select("id").eq("type", "person").ilike("label", "Danny").maybe_single().execute()
    if not danny_res or not danny_res.data:
        print("Could not find Danny node.")
        return
    danny_id = danny_res.data["id"]

    # Delete garbage 'User' node
    try:
        supabase.table("graph_nodes").delete().eq("label", "User").execute()
    except Exception:
        pass

    # Find 0-edge and AUTHORED-only nodes (excluding tasks)
    all_nodes = fetch_all_paginated("graph_nodes", "id, label, type")
    all_edges = fetch_all_paginated("graph_edges", "id, source_node_id, target_node_id, relationship")
    
    # Build degree map
    node_edges = {n["id"]: [] for n in (all_nodes or [])}
    for e in (all_edges or []):
        if e["source_node_id"] in node_edges:
            node_edges[e["source_node_id"]].append(e)
        if e["target_node_id"] in node_edges:
            node_edges[e["target_node_id"]].append(e)

    fixed_count = 0
    
    type_to_rel = {
        "project": "OWNS",
        "person": "KNOWS",
        "concept": "INTERESTED_IN",
        "organization": "WORKS_WITH",
        "pet/dog": "OWNS",
        "emotional_state": "FEELS",
        "resource": "USES",
        "cluster": "OWNS"
    }

    edges_to_insert = []
    edges_to_delete = []

    for node in (all_nodes or []):
        if node["id"] == danny_id or node["type"] == "task":
            continue
            
        edges = node_edges.get(node["id"], [])
        
        # Find edges that connect directly to Danny
        danny_edges = [
            e for e in edges 
            if e["source_node_id"] == danny_id or e["target_node_id"] == danny_id
        ]
        
        needs_fix = False
        
        if not danny_edges:
            # No direct connection to Danny at all
            needs_fix = True
        else:
            # Has connections to Danny. Are they ALL just "AUTHORED"?
            non_authored = [e for e in danny_edges if e["relationship"].upper() != "AUTHORED"]
            if not non_authored:
                # All edges to Danny are weak "AUTHORED" edges. Upgrade them.
                needs_fix = True
                edges_to_delete.extend([e["id"] for e in danny_edges])

        if needs_fix:
            rel = type_to_rel.get(node["type"], "RELATES_TO")
            edges_to_insert.append({
                "source_label": "Danny",
                "target_label": node["label"],
                "relationship": rel,
                "source_text": "backfill_orphaned_node_edges",
                "status": "pending"
            })
            fixed_count += 1

    # Execute deletions for upgraded AUTHORED edges
    if edges_to_delete:
        for i in range(0, len(edges_to_delete), 100):
            batch = edges_to_delete[i:i+100]
            try:
                supabase.table("graph_edges").delete().in_("id", batch).execute()
            except Exception as e:
                print(f"Failed to delete batch of AUTHORED edges: {e}")

    # Queue insertions
    if edges_to_insert:
        insert_pending_edges_batch(edges_to_insert)

    print(f"✅ Fixed {fixed_count} isolated/AUTHORED-only nodes.")


def sync_project_nodes_to_projects_table():
    """Sync project-type graph nodes to projects table via legacy_id.
    For each project node missing legacy_id, match by label to projects table.
    One-time backfill for existing orphan data, then runs incrementally."""
    print("\n🏗️ Project node sync: Linking graph projects to projects table...")
    nodes = fetch_all_paginated("graph_nodes", "id, label, metadata", in_filter_col="type", in_filter_val=["project"])
    if not nodes:
        print("No project nodes found.")
        return

    all_projects = fetch_all_paginated("projects", "id, name")
    name_to_id = {p["name"].strip().lower(): p["id"] for p in all_projects}

    synced = 0
    for n in nodes:
        meta = _normalize_meta(n.get("metadata"))
        if meta.get("legacy_id"):
            continue
        label_lower = n["label"].strip().lower()
        legacy_id = name_to_id.get(label_lower)
        if legacy_id:
            meta["legacy_id"] = legacy_id
            try:
                supabase.table("graph_nodes").update({"metadata": meta}).eq("id", n["id"]).execute()
                synced += 1
            except Exception as e:
                audit_log_sync("backfill_graph", "WARNING", f"Failed to sync project node {n['id']}: {e}")

    print(f"Synced {synced} project nodes to projects table.")


def sync_person_nodes_to_people_table():
    """Sync person-type graph nodes to people table via people_id.
    For each person node missing people_id, match by label to people table.
    Creates new people table rows for unmatched person nodes."""
    print("\n👤 Person node sync: Linking graph people to people table...")
    nodes = fetch_all_paginated("graph_nodes", "id, label, type, metadata", in_filter_col="type", in_filter_val=["person"])
    if not nodes:
        print("No person nodes found.")
        return

    all_people = fetch_all_paginated("people", "id, name")
    name_to_id = {}
    for p in all_people:
        raw = p["name"].strip().lower()
        if raw and raw not in name_to_id:
            name_to_id[raw] = p["id"]
        norm = normalize_person_name(p["name"])
        if norm and norm not in name_to_id:
            name_to_id[norm] = p["id"]

    synced = 0
    added = 0
    skipped = 0
    for n in nodes:
        # Defensive: skip if node type is not person (safety guard for type corruption)
        if n.get("type") != "person":
            skipped += 1
            continue
        meta = _normalize_meta(n.get("metadata"))
        if meta.get("people_id"):
            continue
        if is_blocklisted_person(n["label"]):
            skipped += 1
            continue
        label_lower = n["label"].strip().lower()
        label_norm = normalize_person_name(n["label"])
        matched_id = name_to_id.get(label_norm) or name_to_id.get(label_lower)
        if matched_id:
            meta["people_id"] = matched_id
        else:
            try:
                result = supabase.table("people").insert({
                    "name": n["label"].strip(),
                    "source": "backfill_graph"
                }).execute()
                if result.data:
                    new_id = result.data[0]["id"]
                    meta["people_id"] = new_id
                    if label_norm:
                        name_to_id[label_norm] = new_id
                    added += 1
                else:
                    continue
            except Exception as e:
                audit_log_sync("backfill_graph", "WARNING", f"Failed to create person '{n['label']}': {e}")
                continue
        try:
            supabase.table("graph_nodes").update({"metadata": meta}).eq("id", n["id"]).execute()
            synced += 1
        except Exception as e:
            audit_log_sync("backfill_graph", "WARNING", f"Failed to update person node {n['id']}: {e}")

    print(f"Synced {synced} person nodes ({added} new people, {skipped} blocklisted).")



def dedup_graph_nodes(dry_run: bool = True):
    """
    Tier 3: Deduplicate case-variant graph nodes (e.g., guilt vs Guilt).
    Canonical node selection:
      1. Title Case variant
      2. If tied, oldest created_at (or just first in sorted order if created_at not fetched)
    """
    print(f"\n🧹 Node Dedup {'(DRY RUN)' if dry_run else '(LIVE RUN)'}: Merging case-variant duplicates...")
    
    # Fetch all nodes
    nodes = fetch_all_paginated("graph_nodes", "id, label, type")
    if not nodes:
        print("No nodes found.")
        return
        
    # Group by lowercase label
    groups = {}
    for n in nodes:
        key = n["label"].strip().lower()
        if not key:
            continue
        if key not in groups:
            groups[key] = []
        groups[key].append(n)
        
    merge_count = 0
    edge_repoint_count = 0
    deleted_nodes_count = 0
    deleted_duplicate_edges = 0
    
    for key, group in groups.items():
        if len(group) <= 1:
            continue
            
        # Determine canonical node
        # Sort by: Is Title Case? (True first), then ID (as stable fallback)
        def sort_key(node):
            is_title = node["label"] == node["label"].title()
            return (not is_title, node["id"])
            
        group.sort(key=sort_key)
        canonical = group[0]
        duplicates = group[1:]
        
        print(f"\nGroup: '{key}' ({len(group)} nodes)")
        print(f"  Canonical: {canonical['label']} ({canonical['id']}, type: {canonical['type']})")
        
        for dup in duplicates:
            print(f"  Duplicate: {dup['label']} ({dup['id']}, type: {dup['type']}) -> merging into canonical")
            merge_count += 1
            
            if not dry_run:
                try:
                    # 1. Repoint source edges
                    source_edges_res = supabase.table("graph_edges").select("id, target_node_id, relationship").eq("source_node_id", dup["id"]).execute()
                    if source_edges_res.data:
                        for edge in source_edges_res.data:
                            # Check if canonical already has this edge
                            check = supabase.table("graph_edges").select("id").eq("source_node_id", canonical["id"]).eq("target_node_id", edge["target_node_id"]).eq("relationship", edge["relationship"]).execute()
                            if check.data:
                                # Conflict: delete the duplicate's edge
                                supabase.table("graph_edges").delete().eq("id", edge["id"]).execute()
                                deleted_duplicate_edges += 1
                            else:
                                # Safe to repoint
                                supabase.table("graph_edges").update({"source_node_id": canonical["id"]}).eq("id", edge["id"]).execute()
                                edge_repoint_count += 1
                                
                    # 2. Repoint target edges
                    target_edges_res = supabase.table("graph_edges").select("id, source_node_id, relationship").eq("target_node_id", dup["id"]).execute()
                    if target_edges_res.data:
                        for edge in target_edges_res.data:
                            check = supabase.table("graph_edges").select("id").eq("target_node_id", canonical["id"]).eq("source_node_id", edge["source_node_id"]).eq("relationship", edge["relationship"]).execute()
                            if check.data:
                                supabase.table("graph_edges").delete().eq("id", edge["id"]).execute()
                                deleted_duplicate_edges += 1
                            else:
                                supabase.table("graph_edges").update({"target_node_id": canonical["id"]}).eq("id", edge["id"]).execute()
                                edge_repoint_count += 1

                    # 3. Delete duplicate node
                    supabase.table("graph_nodes").delete().eq("id", dup["id"]).execute()
                    deleted_nodes_count += 1
                except Exception as e:
                    audit_log_sync("backfill_graph", "ERROR", f"Dedup failed for {dup['id']}: {e}")
                    
    print("\n📊 Dedup Summary:")
    print(f"Nodes merged: {merge_count}")
    if not dry_run:
        print(f"Edges repointed: {edge_repoint_count}")
        print(f"Nodes deleted: {deleted_nodes_count}")
        print(f"Duplicate edges deleted: {deleted_duplicate_edges}")
        
if __name__ == "__main__":
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    if not supabase_url or not supabase_key:
        print("ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)
    
    # Run backfill
    run_backfill()
    
    # Run graph→table sync
    sync_project_nodes_to_projects_table()
    sync_person_nodes_to_people_table()
    
    print("✅ All Phase-2 operations complete")
