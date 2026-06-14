from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()  # CALL it before other imports!

from core.services.db import get_supabase  # noqa: E402
from core.skills.backfill_graph import (  # noqa: E402
    extract_graph_elements, 
    fetch_known_entities, 
    upsert_nodes,
    fetch_pending_entities,
    insert_pending_edges_batch, 
    synthesize_content,
    MEMORY_TYPES,
    _normalize_meta,
    fetch_all_paginated
)
from core.lib.audit_logger import audit_log_sync  # noqa: E402

load_dotenv()

supabase = get_supabase()

def run_one_time_backfill():
    print("Fetching existing processed edges...")
    
    # 1. Get processed memory IDs from graph_edges
    existing_edges = fetch_all_paginated("graph_edges", "metadata")
    processed_ids = set()
    for row in existing_edges or []:
        meta = _normalize_meta(row.get("metadata"))
        if meta.get("memory_id"):
            try:
                processed_ids.add(int(meta["memory_id"]))
            except (ValueError, TypeError):
                pass
                
    # 2. Get processed memory IDs from pending_graph_edges
    pending_edges = fetch_all_paginated("pending_graph_edges", "source_text")
    for row in pending_edges or []:
        st = row.get("source_text", "")
        if st and st.startswith("memories:"):
            try:
                processed_ids.add(int(st.split(":")[1]))
            except (ValueError, IndexError):
                pass
                
    print(f"Found {len(processed_ids)} already processed memory IDs (live + pending).")
    
    # 3. Fetch target memories
    memories = fetch_all_paginated("memories", "id, content, memory_type, metadata", "memory_type", MEMORY_TYPES)
    filtered_memories = [m for m in (memories or []) if 'http://' not in str(m.get('content', '')).lower() and 'https://' not in str(m.get('content', '')).lower()]
    
    unprocessed = [m for m in filtered_memories if m['id'] not in processed_ids]
    
    print(f"Found {len(unprocessed)} TRULY unprocessed memories.")
    if not unprocessed:
        print("Nothing to do!")
        return

    known_entities = fetch_known_entities()
    print(f"Loaded {len(known_entities)} known entities for grounding.")
    
    graph_entities = {}
    nodes_res = fetch_all_paginated("graph_nodes", "id, label, type")
    for n in nodes_res or []:
        graph_entities[n['label']] = {"id": n['id'], "type": n['type']}
        
    fetch_pending_entities()
    
    print("Starting batched parallel processing (8 workers). Rate limiter will manage RPM.")
    processed_count = 0
    failed_count = 0
    BATCH_SIZE = 25
    
    for i in range(0, len(unprocessed), BATCH_SIZE):
        batch = unprocessed[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"\nProcessing batch {batch_num} ({len(batch)} memories)...")
        
        extracted_data = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_mem = {
                executor.submit(
                    extract_graph_elements, synthesize_content(m), m["id"], known_entities
                ): m for m in batch if synthesize_content(m).strip()
            }
            
            for future in as_completed(future_to_mem):
                mem = future_to_mem[future]
                try:
                    graph_data = future.result()
                    nodes = graph_data.get("nodes", [])
                    edges = graph_data.get("edges", [])
                    
                    if not nodes and not edges:
                        print(f"  Memory {mem['id']}: 0 nodes, 0 edges extracted.")
                        try:
                            supabase.table("pending_graph_edges").insert({
                                "source_label": "",
                                "target_label": "",
                                "relationship": "SKIPPED",
                                "source_text": f"memories:{mem['id']}",
                                "source_table": "memories",
                                "status": "skipped"
                            }).execute()
                        except Exception:
                            pass
                        failed_count += 1
                        continue
                        
                    print(f"  Memory {mem['id']}: Extracted {len(nodes)} nodes, {len(edges)} edges.")
                    extracted_data.append({
                        "memory_id": mem["id"], 
                        "source_table": mem.get("_source_table", "memories"), 
                        "nodes": nodes, 
                        "edges": edges
                    })
                    processed_count += 1
                    
                except Exception as e:
                    print(f"  ❌ Failed memory {mem['id']}: {e}")
                    audit_log_sync("one_time_backfill", "ERROR", f"Failed memory {mem['id']}: {e}")
                    try:
                        supabase.table("pending_graph_edges").insert({
                            "source_label": "",
                            "target_label": "",
                            "relationship": "FAILED",
                            "source_text": f"memories:{mem['id']}",
                            "source_table": "memories",
                            "status": "failed"
                        }).execute()
                    except Exception:
                        pass
                    failed_count += 1
                    
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
                    "memory_id": data["memory_id"], 
                    "source_table": data["source_table"]
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
            
        # Batch upsert nodes
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
            
        # ⚠️ GUARANTEED SENTINEL FIX ⚠️
        # If all edges for a memory get auto-rejected by validation (e.g. BANNED KNOWS/WORKS_WITH),
        # no DB row is written, so the memory gets reprocessed infinitely.
        # We write a guaranteed 'skipped' sentinel for EVERY processed memory here.
        guaranteed_sentinels = []
        for data in extracted_data:
            guaranteed_sentinels.append({
                "source_label": "",
                "target_label": "",
                "relationship": "SENTINEL",
                "source_text": f"{data['source_table']}:{data['memory_id']}",
                "source_table": data['source_table'],
                "status": "skipped"
            })
        if guaranteed_sentinels:
            # We use supabase insert directly because insert_pending_edges_batch does deduplication 
            # and might have logic that messes with sentinels.
            try:
                for i in range(0, len(guaranteed_sentinels), 100):
                    batch_sentinels = guaranteed_sentinels[i:i+100]
                    supabase.table("pending_graph_edges").insert(batch_sentinels).execute()
            except Exception as e:
                print(f"Failed to insert guaranteed sentinels: {e}")
            
        print(f"✅ Completed batch {batch_num}")

    print(f"\n✅ Finished processing {processed_count} memories successfully (Skipped/Failed: {failed_count}).")

if __name__ == "__main__":
    run_one_time_backfill()
