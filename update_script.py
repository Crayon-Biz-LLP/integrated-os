
with open('core/skills/backfill_graph.py', 'r') as f:
    content = f.read()

# Add import at the top
content = content.replace('import json\n', 'import json\nfrom concurrent.futures import ThreadPoolExecutor, as_completed\n')

# 1. Replace run_backfill
run_start = content.find("def run_backfill():")
run_end = content.find("def backfill_orphaned_tasks():")

new_run_backfill = """def run_backfill():
    # ── Step 1: Patch missing embeddings first ──────────────────────────────
    backfill_embeddings()

    # ── Step 2: Backfill graph edges ────────────────────────────────────────
    print("\\n🔗 Graph backfill: fetching memories for graph edges...")
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

"""

content = content[:run_start] + new_run_backfill + "\n\n" + content[run_end:]

# 2. Remove Phase-3 unused functions
p3_start = content.find("def compact_memories(supabase):")
if p3_start != -1:
    main_block = """
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
"""
    content = content[:p3_start] + main_block

with open('core/skills/backfill_graph.py', 'w') as f:
    f.write(content)
