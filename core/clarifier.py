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
        
    # Check if this label pair has already been resolved or asked about.
    # This suppresses duplicate low-confidence clarifications and false-positive
    # contradictions for relationship variants.
    already_handled = supabase.table("pending_graph_edges")\
        .select("id")\
        .eq("source_label", from_lbl)\
        .eq("target_label", to_lbl)\
        .neq("status", "pending")\
        .limit(1)\
        .execute()
    if already_handled and already_handled.data:
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
    """Process user reply to a clarification question (c{shortcode}).
    
    Handles free-form answers — grounding questions accept any context,
    other question types use expanded positive/negative word sets.
    """
    supabase = get_supabase()
    
    res = supabase.table("clarification_feedback").select("*").eq("shortcode", shortcode).maybe_single().execute()
    if not res.data:
        return {"status": "error", "message": "Shortcode not found"}
        
    feedback = res.data
    source_table = feedback["source_table"]
    source_id = feedback["source_id"]
    question_type = feedback.get("question_type", "")
    
    POSITIVE = {'y', 'yes', 'yeah', 'yep', 'approve', 'correct', 'right', 'true',
                'sure', 'ok', 'okay', 'do it', 'add it', "that's right", 'thats right',
                'looks good', 'confirm', 'agreed', 'go ahead'}
    NEGATIVE = {'n', 'no', 'nope', 'nah', 'reject', 'wrong', 'incorrect', 'false',
                'skip', 'dismiss', 'drop', 'cancel', 'none', 'leave it', 'neither', 'nope'}
    
    cleaned = answer.strip().lower()
    context = None
    
    if cleaned in POSITIVE:
        response_type = "approved"
    elif cleaned in NEGATIVE:
        response_type = "rejected"
    else:
        if question_type == "grounding":
            response_type = "approved"
            context = answer
        else:
            response_type = "rejected"
    
    update = {
        "answer": answer,
        "response_type": response_type,
        "resolved_at": "now()"
    }
    if context:
        update["context"] = context
    
    supabase.table("clarification_feedback").update(update).eq("shortcode", shortcode).execute()
    
    source_update = {
        "clarification_status": "resolved",
        "status": "approved" if response_type == "approved" else "rejected"
    }
    if context:
        source_update["clarification_answer"] = context
    
    supabase.table(source_table).update(source_update).eq("id", source_id).execute()
    
    # If an edge was approved via clarification, promote it to the permanent graph_edges table
    if source_table == "pending_graph_edges" and response_type == "approved":
        pe_res = supabase.table("pending_graph_edges").select("*").eq("id", source_id).maybe_single().execute()
        if pe_res and pe_res.data:
            pe = pe_res.data
            s_node = supabase.table("graph_nodes").select("id").eq("label", pe["source_label"]).maybe_single().execute()
            t_node = supabase.table("graph_nodes").select("id").eq("label", pe["target_label"]).maybe_single().execute()
            if s_node and s_node.data and t_node and t_node.data:
                meta = {"source": "clarification_approval", "pending_id": source_id}
                if context:
                    meta["context"] = context
                if pe.get("source_text"):
                    memories = [m.strip() for m in pe["source_text"].split(",") if m.strip()]
                    if memories:
                        meta["contributing_memories"] = memories
                
                supabase.table("graph_edges").upsert({
                    "source_node_id": s_node.data["id"],
                    "target_node_id": t_node.data["id"],
                    "relationship": pe["relationship"],
                    "weight": 1.0,
                    "metadata": meta
                }, on_conflict="source_node_id,relationship,target_node_id", ignore_duplicates=True).execute()
    
    return {"status": "ok", "action": response_type, "context": context}

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

