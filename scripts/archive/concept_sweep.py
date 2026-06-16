#!/usr/bin/env python3
import json
from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402
from core.llm.constants import CLASSIFICATION_MODEL
from core.llm.compat import call_llm_with_fallback_sync
from core.skills.backfill_graph import synthesize_content
from core.clarifier import evaluate_node  # noqa: E402

supabase = get_supabase()

def fetch_all_memories():
    res = supabase.table("memories").select("*").execute()
    return res.data or []

def extract_concepts_only(memory: dict) -> dict:
    import re
    text = synthesize_content(memory)
    cleaned_text = re.sub(r'\[RESOURCE\].*?(\n|$)', '', text, flags=re.IGNORECASE)
    cleaned_text = re.sub(r'\[CLUSTER\].*?(\n|$)', '', cleaned_text, flags=re.IGNORECASE)
    cleaned_text = re.sub(r'https?://\S+', '', cleaned_text)
    
    prompt = f"""Extract ONLY conceptual associations from this memory.

Return a JSON object with:
- "nodes": array of objects with {{"label": string, "type": "concept", "epistemic": "inferred", "justification": string}}
- "edges": array of objects with {{"source": string, "target": string, "relationship": string, "epistemic": "inferred", "justification": string}}

Text: {cleaned_text}

Rules:
- CONCEPTUAL ASSOCIATIONS (extract sparingly — at most 2 per memory):
  Extract abstract concepts this memory evokes. Concepts are intangible themes,
  not people, places, or projects.
  Good: "cash flow urgency", "execution risk", "trust repair", "delivery pressure"
  Bad:  "meeting", "project update" (too generic), "Qhord" (that's a project node)
  
- For each concept:
  - Node: {{"label": "<concept>", "type": "concept", "epistemic": "inferred", "justification": "..."}}
  - Edge from relevant project/person/event to concept:
    {{"source": "<entity>", "target": "<concept>", "relationship": "EVOKES"|"ASSOCIATED_WITH"|"RELATES_TO", "epistemic": "inferred", "justification": "..."}}
    
- If nothing strong surfaces, extract zero concepts. Silence is correct.
- EVERY concept must have an edge connecting it to another entity mentioned in the text.
- Do NOT extract regular people/projects/orgs as new nodes here, ONLY concepts. 
  (However, include them in the 'source' of the edge so we can link the concept to them).
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
        print(f"Error extracting memory {memory['id']}: {e}")
    return {"nodes": [], "edges": []}

def run_sweep():
    memories = fetch_all_memories()
    print(f"Sweeping {len(memories)} memories for concepts...")
    
    for mem in memories:
        extracted = extract_concepts_only(mem)
        nodes = extracted.get("nodes", [])
        edges = extracted.get("edges", [])
        
        if not nodes and not edges:
            continue
            
        print(f"Memory {mem['id']}: Found {len(nodes)} concepts, {len(edges)} edges")
        
        # Insert concept nodes
        concept_labels = set()
        for node in nodes:
            if node.get("type") != "concept":
                continue
            concept_labels.add(node["label"])
            
            clarification = evaluate_node(node, batch_mode=True)
            if clarification:
                # Queue clarification
                try:
                    supabase.table("pending_graph_nodes").insert({
                        "label": node["label"],
                        "type": "concept",
                        "status": "flagged",
                        "source_text": f"memory_{mem['id']}",
                        "epistemic_status": node.get("epistemic", "inferred"),
                        "eval_context": {"justification": node.get("justification", "")}
                    }).execute()
                    print(f"  Flagged concept: {node['label']} (Similarity alert)")
                except Exception:
                    print(f"  Skipped concept {node['label']} (likely exists in pending)")
            else:
                # Insert pending node
                try:
                    supabase.table("pending_graph_nodes").insert({
                        "label": node["label"],
                        "type": "concept",
                        "status": "pending",
                        "source_text": f"memory_{mem['id']}",
                        "epistemic_status": node.get("epistemic", "inferred"),
                        "eval_context": {"justification": node.get("justification", "")}
                    }).execute()
                    print(f"  Queued concept: {node['label']}")
                except Exception:
                    print(f"  Skipped concept {node['label']} (likely exists in pending)")
                
        # Insert edges
        for edge in edges:
            rel = edge.get("relationship", "EVOKES")
            if rel not in ["EVOKES", "RELATES_TO", "ASSOCIATED_WITH"]:
                continue
                
            # We don't have types for the sources easily here, but we can assume validation logic will handle it at approval time,
            # or we bypass strict type validation for the pending queue and let it fail on promotion if invalid.
            # To be safe, we just push it to pending.
            supabase.table("pending_graph_edges").insert({
                "source_label": edge.get("source"),
                "target_label": edge.get("target"),
                "relationship": rel,
                "status": "pending",
                "source_text": f"memory_{mem['id']}",
                "epistemic_status": edge.get("epistemic", "inferred"),
                "eval_context": {"justification": edge.get("justification", "")}
            }).execute()

if __name__ == "__main__":
    run_sweep()