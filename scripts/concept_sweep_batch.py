#!/usr/bin/env python3
import json
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

from core.services.db import get_supabase, maybe_single_safe  # noqa: E402
from core.llm.constants import CLASSIFICATION_MODEL  # noqa: E402
from core.llm.compat import call_llm_with_fallback_sync  # noqa: E402
from core.skills.backfill_graph import synthesize_content  # noqa: E402
from core.clarifier import evaluate_node  # noqa: E402
from core.lib.graph_rules import normalize_label  # noqa: E402

supabase = get_supabase()

def fetch_unprocessed_memories():
    # Fetch all memories
    memories_res = supabase.table("memories").select("*").execute()
    all_memories = memories_res.data or []
    
    # Fetch processed logs
    logs_res = supabase.table("audit_logs").select("metadata").eq("service", "concept_sweep").eq("level", "info").execute()
    processed_ids = {log["metadata"]["source_id"] for log in (logs_res.data or []) if isinstance(log.get("metadata"), dict) and log["metadata"].get("source_id")}
    
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
            model=CLASSIFICATION_MODEL,
            config={"response_mime_type": "application/json"},
            is_critical=False,
            require_json=True
        )
        if hasattr(response, 'text') and response.text:
            return json.loads(response.text)
    except Exception as e:
        print(f"Error extracting batch: {e}")
    return []

def ensure_memory_node_exists(memory_id: str, content: str) -> str:
    """Ensure a memory graph node exists, creating one on-the-fly if not. Returns the node label."""
    memory_label = f"Memory {memory_id}"
    try:
        existing = maybe_single_safe(
            supabase.table("graph_nodes")
            .select("id")
            .eq("type", "memory")
            .eq("db_record_id", str(memory_id))
        )
        if existing and existing.data:
            return memory_label
            
        from core.lib.graph_rules import make_memory_preview
        preview = make_memory_preview(content)
        meta = {"source": "concept-sweep-fallback", "preview": preview or content[:100]}
        
        supabase.table("graph_nodes").insert({
            "label": memory_label,
            "type": "memory",
            "normalized_label": normalize_label(memory_label),
            "db_record_id": str(memory_id),
            "epistemic_status": "asserted",
            "metadata": meta
        }).execute()
        print(f"    Created fallback memory node for {memory_id}")
    except Exception as e:
        print(f"    Warning: Failed to ensure memory node for {memory_id}: {e}")
    return memory_label


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
                        supabase.table("pending_nodes").insert({
                            "label": label,
                            "type": "concept",
                            "status": "flagged",
                            "source_text": f"memories:{mem_id}",
                            "epistemic_status": node.get("epistemic", "inferred"),
                            "eval_context": eval_ctx
                        }).execute()
                        print(f"    Flagged concept: {label} (Similarity alert)")
                    except Exception as e:
                        if hasattr(e, "code") and e.code == "23505":
                            print(f"    Concept already flagged: {label}")
                        else:
                            print(f"    Failed to flag concept {label}: {e}")
                else:
                    try:
                        supabase.table("pending_nodes").insert({
                            "label": label,
                            "type": "concept",
                            "status": "pending",
                            "source_text": f"memories:{mem_id}",
                            "epistemic_status": node.get("epistemic", "inferred"),
                            "eval_context": eval_ctx
                        }).execute()
                        print(f"    Queued concept: {label}")
                    except Exception as e:
                        if hasattr(e, "code") and e.code == "23505":
                            print(f"    Concept already queued: {label}")
                        else:
                            print(f"    Failed to queue concept {label}: {e}")
                        
            # Insert edges
            from core.lib.graph_rules import insert_pending_edge
            for edge in edges:
                rel = edge.get("relationship", "EVOKES")
                res = insert_pending_edge(
                    edge.get("source"),
                    edge.get("target"),
                    rel,
                    {
                        "source_text": f"memories:{mem_id}",
                        "source_type": "concept",
                        "target_type": "concept"
                    }
                )
                if res.get("status") == "error":
                    print(f"    Failed to insert edge: {res.get('reason')}")

            # Backfill linked_entity for concept nodes by querying the edges just inserted
            for node in nodes:
                if node.get("type") != "concept":
                    continue
                label = node["label"]
                try:
                    edge_res = maybe_single_safe(
                        supabase.table("pending_graph_edges")
                        .select("source_label, relationship")
                        .ilike("target_label", label)
                        .eq("source_text", f"memories:{mem_id}")
                        .eq("status", "pending")
                    )
                        
                    if edge_res and edge_res.data:
                        linked_entity = edge_res.data["source_label"]
                        relationship = edge_res.data["relationship"]

                        # Verify the linked_entity exists as a graph node
                        entity_exists = maybe_single_safe(
                            supabase.table("graph_nodes").select("id")
                            .ilike("label", linked_entity)
                        )

                        if not entity_exists or not entity_exists.data:
                            # Fallback: ensure memory node exists and use it as anchor
                            ensure_memory_node_exists(mem_id, mem.get("content", ""))
                            memory_label = f"Memory {mem_id}"
                            linked_entity = memory_label
                            relationship = "EVOKES"
                            print(f"    Fallback: linking concept '{label}' to memory node '{memory_label}'")

                        eval_ctx = {
                            "justification": node.get("justification", ""),
                            "linked_entity": linked_entity,
                            "relationship": relationship
                        }
                        supabase.table("pending_nodes")\
                            .update({"eval_context": eval_ctx})\
                            .ilike("label", label)\
                            .eq("source_text", f"memories:{mem_id}")\
                            .in_("status", ["pending", "flagged"])\
                            .execute()
                except Exception as e:
                    print(f"    Warning: Failed to backfill linked_entity for {label}: {e}")
            
            # Log as processed
            try:
                supabase.table("audit_logs").insert({
                    "service": "concept_sweep",
                    "level": "info",
                    "message": f"Completed memory {mem_id}",
                    "metadata": {
                        "source_table": "memories",
                        "source_id": mem_id,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "concepts_extracted": len(nodes)
                    }
                }).execute()
            except Exception as e:
                print(f"    Warning: Failed to write audit_logs for {mem_id}: {e}")
                
if __name__ == "__main__":
    run_batch_sweep()