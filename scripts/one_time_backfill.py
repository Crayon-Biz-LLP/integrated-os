import time
from dotenv import load_dotenv

from core.services.db import get_supabase
from core.skills.backfill_graph import (
    extract_graph_elements, 
    fetch_known_entities, 
    get_or_create_node, 
    insert_pending_edges_batch, 
    synthesize_content,
    MEMORY_TYPES,
    _normalize_meta,
    fetch_all_paginated
)
from core.lib.audit_logger import audit_log_sync

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
        
    created_nodes = {}
    
    print("Starting strictly sequential processing at 10 RPM (6s sleep between LLM calls).")
    processed_count = 0
    
    for mem in unprocessed:
        content = synthesize_content(mem)
        if not content.strip():
            continue
            
        print(f"\nProcessing memory {mem['id']} ({processed_count+1}/{len(unprocessed)})...")
        
        try:
            # 1. Extract (LLM Call)
            graph_data = extract_graph_elements(content, mem["id"], known_entities)
            
            # 2. Sleep exactly 6.5s to ensure we stay under 15 RPM
            time.sleep(6.5)
            
            nodes = graph_data.get("nodes", [])
            edges = graph_data.get("edges", [])
            
            if not nodes and not edges:
                # Mark as processed anyway by inserting a dummy pending edge so we don't process it again
                # Actually, if there are no edges, it will be skipped next time anyway if we don't track it?
                # The logic fetches from pending_graph_edges. If no edges, it won't be there!
                # We can't insert a dummy edge, so let's just write to audit logs and skip. It might be re-processed in future, 
                # but for this script it's fine.
                print("  0 nodes, 0 edges extracted.")
                continue
                
            print(f"  Extracted {len(nodes)} nodes, {len(edges)} edges.")
            
            # 3. Create nodes
            for node in nodes:
                get_or_create_node(node.get("label", ""), node.get("type", "concept"), graph_entities, created_nodes, f"memories:{mem['id']}")
                
            # 4. Create pending edges
            edges_to_insert = []
            for edge in edges:
                edges_to_insert.append({
                    "source_label": edge.get("source", ""),
                    "target_label": edge.get("target", ""),
                    "relationship": edge.get("relationship", "").upper(),
                    "source_text": f"memories:{mem['id']}",
                    "source_table": mem.get("_source_table", "memories"),
                    "status": "pending"
                })
                
            if edges_to_insert:
                insert_pending_edges_batch(edges_to_insert)
            
            processed_count += 1
            
        except Exception as e:
            print(f"  ❌ Failed memory {mem['id']}: {e}")
            audit_log_sync("one_time_backfill", "ERROR", f"Failed memory {mem['id']}: {e}")
            # If it's a 500 error, we still sleep to prevent hammering
            time.sleep(6.5)

    print(f"\n✅ Finished processing {processed_count} memories.")

if __name__ == "__main__":
    run_one_time_backfill()
