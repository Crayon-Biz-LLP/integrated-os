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
    "Prophecy", "Psalm", "Prayer", "Journal", "Sermon",
    "archive", "canonical_page", "note", "outcome", "reflection",
    "relationship_note"
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
    print(f"    Memories matching MEMORY_TYPES filter: {len(memories) if memories else 0}")
    
    # Count by type
    if memories:
        type_counts = {}
        for m in memories:
            t = m.get("memory_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"      {t}: {c}")
    
    # Filter to only unprocessed memories (fix int/string type mismatch)
    memories = [m for m in (memories or []) if m["id"] not in processed_memory_ids]
    print(f"    Already in graph edges (skipped): {len(processed_memory_ids)}")
    print(f"    New memories to process: {len(memories)}")
    
    known_entities = fetch_known_entities()

    raw_dumps = fetch_all_paginated("raw_dumps", "id, content, created_at, metadata")
    qualifying_dumps = []
    for d in (raw_dumps or []):
        if d["id"] in processed_memory_ids:
            continue
        content = d.get("content", "")
        meta = _normalize_meta(d.get("metadata"))
        # Include if it has NOTE intent OR contains known entity
        is_note = meta.get("intent") == "NOTE"
        has_entity = dump_contains_known_entity(content, known_entities)
        if is_note or has_entity:
            qualifying_dumps.append({
                "id": d["id"],
                "content": content,
                "memory_type": "raw_dump",
                "metadata": meta,
                "created_at": d.get("created_at")
            })
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


def extract_graph_elements(text: str, memory_id: str) -> dict:
    prompt = f"""Extract knowledge graph elements from this text.
    
Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "person"|"organization"|"project"|"emotional_state"|"concept"}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string}}
    
Text: {text}
    
Rules:
- Extract People (names), Organizations, Projects, Emotional States, Concepts as nodes
- Create edges for relationships between nodes
- Use UPPERCASE relationship types: "RELATES_TO", "PARENT_OF", "WORKS_AT", "BELONGS_TO", "AUTHORED", "INTRODUCED", "VENDOR_TO", "DISCUSSED_WITH"
- Include "AUTHORED" edge from "Danny" to indicate he wrote this memory
- If no clear graph elements, return empty arrays
- CRITICAL: Do NOT extract anything from URLs, file paths, or online handles. Ignore path segments in links like "github.com/username" or "bit.ly/handle". Only extract entities that appear as clear person names, organization names, or project names in natural language text.
- CONSISTENCY: EVERY label referenced in an edge's "source" or "target" MUST also appear in the "nodes" array with its type."""
    
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
                print(f"    Extracted {len(result.get('nodes', []))} nodes, {len(result.get('edges', []))} edges from memory {memory_id}")
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

def get_or_create_node(label: str, node_type: str, graph_entities: dict, created_nodes: dict, memory_id: str = None) -> str:
    """
    Get or create a graph node with proper type handling.
    If node exists, updates its type to match the latest extracted type.
    """
    if label in created_nodes:
        return created_nodes[label]
    
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
    try:
        result = supabase.table("graph_nodes").upsert(
            {"label": label, "type": node_type, "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})},
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
        
        existing = graph_entities.get(label, {})
        existing_id = existing.get("id")
        existing_type = existing.get("type", "concept")
        
        record = {
            "label": label,
            "type": node_type,  # Always use latest extracted type
            "metadata": json.dumps({"source": "backfill_graph", "memory_id": memory_id})
        }
        
        if existing_id:
            record["id"] = existing_id
            # If type changed, update it
            if existing_type != node_type:
                record["type"] = node_type
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


def insert_edges(edges: list, node_label_to_id: dict, memory_id: str):
    """Insert edges with validation - skip if source/target node doesn't exist."""
    orphaned = 0
    
    for edge in edges:
        source_label = edge.get("source", "")
        target_label = edge.get("target", "")
        relationship = edge.get("relationship", "relates_to").upper()
        
        source_id = node_label_to_id.get(source_label)
        target_id = node_label_to_id.get(target_label)
        
        # VALIDATION: Skip if nodes don't exist in DB
        if not source_id or not target_id:
            audit_log_sync("backfill_graph", "WARNING", 
                f"Skipping edge {source_label}->{target_label}: missing node ID")
            orphaned += 1
            continue
        
        # Additional validation: Check if nodes actually exist in DB
        try:
            source_check = supabase.table("graph_nodes").select("id").eq("id", source_id).execute()
            target_check = supabase.table("graph_nodes").select("id").eq("id", target_id).execute()
            
            if not source_check.data or not target_check.data:
                audit_log_sync("backfill_graph", "WARNING", 
                    "Skipping edge: node doesn't exist in DB")
                orphaned += 1
                continue
        except Exception as ve:
            audit_log_sync("backfill_graph", "WARNING", f"Node validation failed: {ve}")
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
        node_type = node.get("type", "concept")
        node_id = get_or_create_node(label, node_type, graph_entities, created_nodes, memory_id)
        if node_id:
            node_label_to_id[label] = node_id
    
    if not node_label_to_id:
        danny_id = get_or_create_node("Danny", "person", graph_entities, created_nodes, memory_id)
        if danny_id:
            node_label_to_id["Danny"] = danny_id
    
    insert_edges(edges, node_label_to_id, memory_id)
    
    return True


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
                    extract_graph_elements, 
                    synthesize_content(m), 
                    m["id"]
                ): m for m in batch if synthesize_content(m).strip()
            }
            
            for future in as_completed(future_to_mem):
                mem = future_to_mem[future]
                try:
                    graph_data = future.result()
                    nodes = graph_data.get("nodes", [])
                    edges = graph_data.get("edges", [])
                    if nodes or edges:
                        extracted_data.append({"memory_id": mem["id"], "nodes": nodes, "edges": edges})
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
                    "memory_id": data["memory_id"]
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
        
        edges_to_insert = []
        for edge in all_edges:
            source_id = graph_entities.get(edge["source"], {}).get("id")
            target_id = graph_entities.get(edge["target"], {}).get("id")
            if source_id and target_id:
                edges_to_insert.append({
                    "source_node_id": source_id,
                    "target_node_id": target_id,
                    "relationship": edge["relationship"],
                    "metadata": json.dumps({"memory_id": str(edge["memory_id"])})
                })
                
        if edges_to_insert:
            try:
                # Upsert all edges in batches of 100 to avoid PostgREST limits
                for j in range(0, len(edges_to_insert), 100):
                    edge_batch = edges_to_insert[j:j+100]
                    supabase.table("graph_edges").upsert(
                        edge_batch, 
                        on_conflict="source_node_id,relationship,target_node_id", 
                        ignore_duplicates=True
                    ).execute()
            except Exception as e:
                audit_log_sync("backfill_graph", "ERROR", f"Batch edge insert failed: {e}")
                
        print(f"Completed batch {batch_num}")
    
    print(f"Graph backfill complete! Processed: {processed}, Skipped: {failed}")

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
    
    orphaned_tasks = [t for t in all_tasks if t["id"] not in task_node_task_ids]
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
                "metadata": json.dumps({"source": "tasks_table", "task_id": task_id, **meta})
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
                    .eq("type", "project") \
                    .filter("metadata->>legacy_id", "eq", str(project_id)) \
                    .maybe_single() \
                    .execute()
            except Exception:
                pass
            # Try metadata->>project_id (old style)
            if proj_node is None or proj_node.data is None:
                try:
                    proj_node = supabase.table("graph_nodes") \
                        .select("id") \
                        .eq("type", "project") \
                        .filter("metadata->>project_id", "eq", str(project_id)) \
                        .maybe_single() \
                        .execute()
                except Exception:
                    proj_node = None
            # Fallback: label-based match using project name
            if (proj_node is None or proj_node.data is None) and project_id in project_id_to_name:
                proj_name = project_id_to_name[project_id]
                try:
                    proj_node = supabase.table("graph_nodes") \
                        .select("id") \
                        .eq("type", "project") \
                        .ilike("label", proj_name) \
                        .maybe_single() \
                        .execute()
                except Exception:
                    proj_node = None
            
            if proj_node is not None and proj_node.data is not None:
                proj_node_id = proj_node.data["id"]
                try:
                    existing = supabase.table("graph_edges") \
                        .select("id") \
                        .eq("source_node_id", task_node_id) \
                        .eq("target_node_id", proj_node_id) \
                        .eq("relationship", "BELONGS_TO") \
                        .maybe_single() \
                        .execute()
                    
                    if existing is None or not existing.data:
                        supabase.table("graph_edges").insert({
                            "source_node_id": task_node_id,
                            "target_node_id": proj_node_id,
                            "relationship": "BELONGS_TO",
                            "weight": 1.0,
                            "metadata": json.dumps({"source": "task_engine", "task_id": task_id})
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
                                "metadata": json.dumps({
                                    "source": "task_engine",
                                    "task_id": task_id,
                                    "matched_name": pname
                                })
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
                supabase.table("graph_nodes").update({"metadata": json.dumps(meta)}).eq("id", n["id"]).execute()
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
            supabase.table("graph_nodes").update({"metadata": json.dumps(meta)}).eq("id", n["id"]).execute()
            synced += 1
        except Exception as e:
            audit_log_sync("backfill_graph", "WARNING", f"Failed to update person node {n['id']}: {e}")

    print(f"Synced {synced} person nodes ({added} new people, {skipped} blocklisted).")



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
