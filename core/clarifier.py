import os
import uuid
from typing import Optional
from supabase import create_client, Client

# Lazy client initialization
def get_supabase() -> Client:
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

def _invoke_llm_evaluation(prompt: str, schema: dict) -> dict:
    """Helper to call LLM for evaluation."""
    # This needs to be sync or we must make evaluate_node async.
    # Since backfill and entity_extractor are sync or async depending on where they are called,
    # let's assume we can run asyncio.run() if there's no loop, but that's risky.
    # We will make evaluate_node and evaluate_edge sync by using a helper or assume they run in an async context?
    # No, wait, backfill_graph is sync! We must use sync LLM call if possible or run in a new loop.
    pass

def evaluate_node(node_data: dict) -> Optional[dict]:
    """Phase 2 implementation. Returns None (silent) for all nodes."""
    return None

def evaluate_edge(edge_data: dict) -> Optional[dict]:
    """Phase 2 implementation. Returns None (silent) for all edges."""
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
    # For now, simplistic parser (stub)
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
    """Phase 2 implementation. Passthrough stub."""
    return items
