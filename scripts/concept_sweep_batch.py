#!/usr/bin/env python3
import json
from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402
from core.skills.backfill_graph import call_llm_with_fallback_sync, synthesize_content  # noqa: E402
from core.clarifier import evaluate_node  # noqa: E402

supabase = get_supabase()

def fetch_unprocessed_memories():
    # Fetch all memories
    memories_res = supabase.table("memories").select("*").execute()
    all_memories = memories_res.data or []
    
    # Fetch processed logs
    logs_res = supabase.table("processing_log").select("source_id").eq("process_type", "concept_sweep").eq("status", "completed").execute()
    processed_ids = {log["source_id"] for log in (logs_res.data or [])}
    
    return [m for m in all_memories if str(m["id"]) not in processed_ids]

def extract_concepts_batch(memories_batch: list) -> dict:
    import re
    
    batch_text = ""
    for i, mem in enumerate(memories_batch, 1):
        text = synthesize_content(mem)
        cleaned_text = re.sub(r'\[RESOURCE\].*?(\n|$)', '', text, flags=re.IGNORECASE)
        cleaned_text = re.sub(r'\[CLUSTER\].*?(\n|$)', '', cleaned_text, flags=re.IGNORECASE)
        cleaned_text = re.sub(r'https?://\S+', '', cleaned_text)
        batch_text += f"\nMEMORY {i} [ID: {mem['id']}]:\n{cleaned_text}\n{'='*40}"
        
        prompt = f"""Extract ONLY conceptual associations from the following batch of memories.

Return a JSON object with a "results" key containing an array where each element corresponds to a memory index:
{{
  "results": [
    {{
      "memory_index": 1,
      "memory_id": "...",
      "nodes": [{{"label": "...", "type": "concept", "epistemic": "inferred", "justification": "..."}}],
      "edges": [{{"source": "...", "target": "...", "relationship": "EVOKES", "epistemic": "inferred", "justification": "..."}}]
    }}
  ]
}}

TEXT TO ANALYZE:
{batch_text}

Rules:
- CONCEPTUAL ASSOCIATIONS (extract sparingly — at most 2 per memory):
  Extract abstract concepts each memory evokes. Concepts are intangible themes,
  not people, places, or projects.
  Good: "cash flow urgency", "execution risk", "trust repair", "delivery pressure"
  Bad:  "meeting", "project update" (too generic), "Qhord" (that's a project node)
  
- For each concept:
  - Node: {{"label": "<concept>", "type": "concept", "epistemic": "inferred", "justification": "..."}}
  - Edge from relevant project/person/event to concept:
    {{"source": "<entity>", "target": "<concept>", "relationship": "EVOKES"|"ASSOCIATED_WITH"|"RELATES_TO", "epistemic": "inferred", "justification": "..."}}
    
- If nothing strong surfaces for a memory, return empty arrays for nodes and edges for that memory. Silence is correct.
- EVERY concept must have an edge connecting it to another entity mentioned in the text.
- Do NOT extract regular people/projects/orgs as new nodes here, ONLY concepts. 
  (However, include them in the 'source' of the edge so we can link the concept to them).
- Make sure to return an entry in the array for EVERY memory index, even if nodes/edges are empty.
"""
    try:
        response = call_llm_with_fallback_sync(
            prompt=prompt,
            model="gemini-3.1-flash-lite",
            config={"response_mime_type": "application/json"},
            is_critical=False,
            require_json=True
        )
        if hasattr(response, 'text') and response.text:
            return json.loads(response.text)
    except Exception as e:
        print(f"Error extracting batch: {e}")
    return []

def run_batch_sweep():
    memories = fetch_unprocessed_memories()
    print(f"Sweeping {len(memories)} unprocessed memories in batches of 5...")
    
    batch_size = 5
    for i in range(0, len(memories), batch_size):
        batch = memories[i:i+batch_size]
        print(f"Processing batch {i//batch_size + 1}/{(len(memories)+batch_size-1)//batch_size} ({len(batch)} memories)...")
        
        results = extract_concepts_batch(batch)
        
        # Build lookup for results by memory_id
        res_by_id = {}
        
        # Handle wrapped results or bare lists
        results_list = results if isinstance(results, list) else (results.get("results", []) if isinstance(results, dict) else [])
        
        for r in results_list:
            if isinstance(r, dict) and "memory_id" in r:
                res_by_id[str(r["memory_id"])] = r
                    
        for mem in batch:
            mem_id = str(mem['id'])
            mem_result = res_by_id.get(mem_id, {})
            nodes = mem_result.get("nodes", [])
            edges = mem_result.get("edges", [])
            
            print(f"  Memory {mem_id}: Found {len(nodes)} concepts, {len(edges)} edges")
            
            # Insert concept nodes
            for node in nodes:
                if node.get("type") != "concept":
                    continue
                
                label = node["label"]
                eval_ctx = {
                    "justification": node.get("justification", ""),
                }
                
                clarification = evaluate_node(node, batch_mode=True)
                if clarification:
                    try:
                        supabase.table("pending_graph_nodes").insert({
                            "label": label,
                            "type": "concept",
                            "status": "flagged",
                            "source_text": f"memories:{mem_id}",
                            "epistemic_status": node.get("epistemic", "inferred"),
                            "eval_context": eval_ctx
                        }).execute()
                        print(f"    Flagged concept: {label} (Similarity alert)")
                    except Exception:
                        pass
                else:
                    try:
                        supabase.table("pending_graph_nodes").insert({
                            "label": label,
                            "type": "concept",
                            "status": "pending",
                            "source_text": f"memories:{mem_id}",
                            "epistemic_status": node.get("epistemic", "inferred"),
                            "eval_context": eval_ctx
                        }).execute()
                        print(f"    Queued concept: {label}")
                    except Exception:
                        pass
                        
            # Insert edges
            for edge in edges:
                rel = edge.get("relationship", "EVOKES")
                if rel not in ["EVOKES", "RELATES_TO", "ASSOCIATED_WITH"]:
                    continue
                    
                try:
                    supabase.table("pending_graph_edges").insert({
                        "source_label": edge.get("source"),
                        "target_label": edge.get("target"),
                        "relationship": rel,
                        "status": "pending",
                        "source_text": f"memories:{mem_id}",
                        "epistemic_status": edge.get("epistemic", "inferred"),
                        "eval_context": {"justification": edge.get("justification", "")}
                    }).execute()
                except Exception:
                    pass

            # Backfill linked_entity for concept nodes by querying the edges just inserted
            for node in nodes:
                if node.get("type") != "concept":
                    continue
                label = node["label"]
                try:
                    edge_res = supabase.table("pending_graph_edges")\
                        .select("source_label, relationship")\
                        .ilike("target_label", label)\
                        .eq("source_text", f"memories:{mem_id}")\
                        .eq("status", "pending")\
                        .maybe_single().execute()
                        
                    if edge_res and edge_res.data:
                        eval_ctx = {
                            "justification": node.get("justification", ""),
                            "linked_entity": edge_res.data["source_label"],
                            "relationship": edge_res.data["relationship"]
                        }
                        supabase.table("pending_graph_nodes")\
                            .update({"eval_context": eval_ctx})\
                            .ilike("label", label)\
                            .eq("source_text", f"memories:{mem_id}")\
                            .in_("status", ["pending", "flagged"])\
                            .execute()
                except Exception as e:
                    print(f"    Warning: Failed to backfill linked_entity for {label}: {e}")
            
            # Log as processed
            try:
                supabase.table("processing_log").upsert({
                    "source_table": "memories",
                    "source_id": mem_id,
                    "process_type": "concept_sweep",
                    "status": "completed",
                    "completed_at": "now()",
                    "concepts_extracted": len(nodes)
                }, on_conflict="source_table,source_id,process_type").execute()
            except Exception as e:
                print(f"    Warning: Failed to write processing_log for {mem_id}: {e}")
                
if __name__ == "__main__":
    run_batch_sweep()