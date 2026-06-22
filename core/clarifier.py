from core.services.db import get_supabase
import uuid
from typing import Optional
from datetime import datetime, timezone, timedelta
from core.lib.graph_rules import find_similar_node, has_structural_anchor

async def store_and_send_clarification(clar: dict, source_table: str, source_id: int | str):
    """
    Store the clarification in DB, update source status, and optionally send via Telegram.
    """
    supabase = get_supabase()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    
    # 1. Update source table FIRST to ensure it's marked as awaiting_clarification
    supabase.table(source_table).update({
        "status": "awaiting_clarification"
    }).eq("id", source_id).execute()
    
    # 2. Insert into clarification_feedback
    supabase.table("clarification_feedback").insert({
        "shortcode": clar["shortcode"],
        "source_table": source_table,
        "source_id": str(source_id),
        "question": clar["question"],
        "question_type": clar["question_type"],
        "expires_at": expires_at
    }).execute()
    
    # 3. Send Telegram if not batch
    if not clar.get("batch"):
        import os
        from core.webhook.telegram import send_telegram
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if chat_id:
            msg = f"🧠 {clar['question']} ({clar['shortcode']})"
            success = await send_telegram(int(chat_id), msg, show_keyboard=False)
            if success:
                supabase.table("clarification_feedback").update({
                    "sent_at": datetime.now(timezone.utc).isoformat()
                }).eq("shortcode", clar["shortcode"]).execute()

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
                "shortcode": next_shortcode(),
                "question_type": "auto_merge",
                "question": f"Did you mean '{top_match['label']}' instead of '{label}'?",
                "target_id": top_match["id"],
                "batch": batch_mode
            }
        else:
            return {
                "shortcode": next_shortcode(),
                "question_type": "disambiguation",
                "question": f"Is '{label}' related to '{top_match['label']}'?",
                "target_id": top_match["id"],
                "batch": batch_mode
            }
            
    if not has_structural_anchor(label, node_type):
        return {
            "shortcode": next_shortcode(),
            "question_type": "grounding",
            "question": f"I don't know who/what '{label}' is. Can you clarify?",
            "batch": batch_mode
        }
        
    return None

def evaluate_edge(edge_data: dict, batch_mode: bool = False) -> Optional[dict]:
    """Phase 2: Evaluate new edge for contradictions or low confidence."""
    supabase = get_supabase()
    from_lbl = edge_data.get("source_label")
    to_lbl = edge_data.get("target_label")
    rel = edge_data.get("relationship", "").upper()
    
    if not from_lbl or not to_lbl:
        return None
        
    s_node = supabase.table("graph_nodes").select("id").eq("label", from_lbl).maybe_single().execute()
    t_node = supabase.table("graph_nodes").select("id").eq("label", to_lbl).maybe_single().execute()
    
    if s_node and s_node.data and t_node and t_node.data:
        existing_edges = supabase.table("graph_edges").select("relationship") \
            .eq("source_node_id", s_node.data["id"]) \
            .eq("target_node_id", t_node.data["id"]) \
            .execute()
            
        if existing_edges and existing_edges.data:
            for ee in existing_edges.data:
                ex_rel = ee["relationship"].upper()
                if ex_rel != rel and ex_rel != "MENTIONS" and rel != "MENTIONS":
                    return {
                        "shortcode": next_shortcode(),
                        "question_type": "edge_contradiction",
                        "question": f'New edge says "{from_lbl} {rel} {to_lbl}" but existing says "{ex_rel}". Which is correct?',
                        "priority": "high",
                        "batch": batch_mode
                    }
                    
    conf = edge_data.get("confidence", 1.0)
    if conf < 0.7:
        return {
            "shortcode": next_shortcode(),
            "question_type": "edge_confidence",
            "question": f'I am unsure if {from_lbl} {rel} {to_lbl}. Is this correct?',
            "priority": "low",
            "batch": batch_mode
        }
    return None

def build_batch(items: list, max_items: int = 5) -> str:
    """Phase 2 implementation. Group multiple clarifications into a single Telegram message."""
    if not items:
        return ""
        
    top = sorted(items, key=lambda c: c.get("priority") == "high", reverse=True)[:max_items]
    msg = f"🧠 {len(top)} things to clarify:\n\n"
    for c in top:
        msg += f"- {c['question']} ({c['shortcode']})\n"
        
    return msg.strip()

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

