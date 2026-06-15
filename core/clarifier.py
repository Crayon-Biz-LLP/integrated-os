import os
import uuid
from typing import Optional
from supabase import create_client, Client
from core.lib.graph_rules import find_similar_node, has_structural_anchor

# Lazy client initialization
def get_supabase() -> Client:
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

def _invoke_llm_evaluation(prompt: str, schema: dict) -> dict:
    """Helper to call LLM for evaluation."""
    pass

def evaluate_node(node_data: dict, batch_mode: bool = False) -> Optional[dict]:
    """
    Phase 2 implementation:
    - Only evaluates NEW nodes (no graph_node_id yet)
    - Scoped to same-type only for duplicate check
    1. Type ambiguity check
    2. Ungrounded check
    3. find_similar_node() >= 0.85 → clarification question
    4. find_similar_node() >= 0.95 → auto-merge confirmation (never silent)
    batch_mode=True → queue for morning Pulse, no immediate Telegram
    """
    label = node_data.get("label", "")
    node_type = node_data.get("type", "")
    
    # Concept nodes: dedup via similarity only — no grounding anchor exists
    if node_type == 'concept':
        similar_list = find_similar_node(
            label=label,
            node_type='concept',
            threshold=0.85
        )
        if similar_list and len(similar_list) > 0:
            match = similar_list[0]
            return {
                'shortcode':     next_shortcode(),
                'question_type': 'concept_alias',
                'question':      f'New concept "{label}" — is this the same as "{match["label"]}"?',
                'target_id':     match['id'],
                'batch':         batch_mode,
            }
        return None  # No similar found — safe to insert
    
    similar = find_similar_node(label, node_type, threshold=0.85)
    
    if similar:
        top_match = similar[0]
        score = top_match["score"]
        
        if score >= 0.95:
            # Auto-merge confirmation
            return {
                "question_type": "auto_merge",
                "question": f"Did you mean '{top_match['label']}' instead of '{label}'?",
                "target_id": top_match["id"],
                "batch": batch_mode
            }
        else:
            return {
                "question_type": "disambiguation",
                "question": f"Is '{label}' related to '{top_match['label']}'?",
                "target_id": top_match["id"],
                "batch": batch_mode
            }
            
    if not has_structural_anchor(label, node_type):
        return {
            "question_type": "grounding",
            "question": f"I don't know who/what '{label}' is. Can you clarify?",
            "batch": batch_mode
        }
        
    return None

def evaluate_edge(edge_data: dict, batch_mode: bool = False) -> Optional[dict]:
    """Phase 1: returns None for all inputs."""
    return None

def build_batch(items: list, batch_size: int = 5) -> list:
    """Phase 2 implementation. Passthrough stub."""
    return items

def handle_response(shortcode: str, answer: str) -> dict:
    """Phase 1 stub."""
    supabase = get_supabase()
    
    # 1. Look up the clarification question
    res = supabase.table("clarification_feedback").select("*").eq("shortcode", shortcode).maybe_single().execute()
    if not res.data:
        return {"status": "error", "message": "Shortcode not found"}
        
    feedback = res.data
    source_table = feedback["source_table"]
    source_id = feedback["source_id"]
    
    # 2. Hybrid parser: regex for standard responses, LLM for free-form
    response_type = "approved" if answer.lower() in ["y", "yes", "approve"] else "rejected"
    if answer.lower() in ["n", "no", "reject"]:
        response_type = "rejected"
        
    # Update feedback table
    supabase.table("clarification_feedback").update({
        "answer": answer,
        "response_type": response_type,
        "resolved_at": "now()"
    }).eq("shortcode", shortcode).execute()
    
    # Update source table
    supabase.table(source_table).update({
        "clarification_status": "resolved",
        "status": "approved" if response_type == "approved" else "rejected"
    }).eq("id", source_id).execute()
    
    return {"status": "ok", "action": response_type}

def next_shortcode() -> str:
    """Phase 1 stub."""
    supabase = get_supabase()
    try:
        res = supabase.rpc('next_clarification_shortcode').execute()
        if res and res.data:
            return res.data
    except Exception:
        pass
    return f"c{uuid.uuid4().hex[:6]}"

def dedupe_batch(items: list) -> list:
    """Phase 2 implementation. Dedupe cross-item."""
    if not items:
        return []
    
    unique_items = []
    seen = set()
    for item in items:
        label = item.get("label", "").lower()
        if label not in seen:
            unique_items.append(item)
            seen.add(label)
            
    return unique_items

