import json
from datetime import datetime
from typing import Dict, Any

from core.webhook.utils import supabase
from core.services.llm import call_gemini_classify
from core.lib.audit_logger import audit_log_sync
from core.pulse.graph import create_graph_node_with_db_record

# In-memory session cache
# Structure: { chat_id: {"actions": [...], "expires_at": datetime, "pending_ids": [...]} }
active_sessions: Dict[int, Dict[str, Any]] = {}

def get_active_session(chat_id: int):
    session = active_sessions.get(chat_id)
    if session:
        if datetime.now() > session['expires_at']:
            del active_sessions[chat_id]
            return None
        return session
    return None

def clear_session(chat_id: int):
    if chat_id in active_sessions:
        del active_sessions[chat_id]

async def interpret_graph_corrections(text: str, pending_items: list) -> list:
    """Uses Gemini to parse natural language corrections into structured actions."""
    if not pending_items:
        return []

    # Format pending items for the prompt
    items_ctx = []
    for item in pending_items:
        items_ctx.append(f"ID: {item['id']} | Label: {item['label']} | Type: {item['type']}")
    
    prompt = f"""
You are a knowledge graph curator. The user has provided natural language corrections for pending graph nodes.
Parse the user's message and determine the action for each mentioned graph node.

Current Pending Items:
{chr(10).join(items_ctx)}

User Message:
{text}

Rules:
1. ONLY return a JSON array of objects.
2. For each node mentioned by the user (e.g. g1, g2), determine if it should be approved or rejected.
3. If the user corrects the name/label, include "corrected_label".
4. If the user corrects the type (e.g. person, project, organization, team), include "corrected_type".
5. If the user indicates the node is a duplicate, alias, or should be merged, map that to action="reject" and include a "reason" explaining why.
6. Allowed actions: "approve", "reject", "skip".
7. If approving a "person" type, check if the user provided context (e.g. role, relationship, organization) and include it as "context".

Return format MUST be a valid JSON array:
[
  {
    "id": 1,
    "action": "approve",
    "corrected_label": "Paulsons Ledgers",
    "corrected_type": "organization"
  },
  {
    "id": 2,
    "action": "approve",
    "corrected_label": "Sarah Johnson",
    "corrected_type": "person",
    "context": "VP Engineering at Equisoft"
  },
  {
    "id": 3,
    "action": "approve",
    "corrected_label": "Qhord Cloud Console",
    "corrected_type": "project"
  }
]
"""
    try:
        response = await call_gemini_classify(
            prompt,
            config={"response_mime_type": "application/json"}
        )
        # Strip markdown fences if present
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
            
        return json.loads(text.strip())
    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Failed to parse graph corrections: {e}")
        return []

async def apply_graph_actions(actions: list, original_items_map: dict) -> dict:
    """Applies the confirmed actions to the database."""
    results = {"applied": 0, "failed": 0, "details": []}
    
    for action in actions:
        node_id = action.get('id')
        if not node_id:
            continue
            
        action_type = action.get('action')
        if action_type not in ('approve', 'reject'):
            continue
            
        original = original_items_map.get(node_id)
        if not original:
            results["failed"] += 1
            results["details"].append(f"g{node_id} failed: not in original pending list")
            continue
            
        # Re-verify status is still pending to prevent race conditions
        try:
            current = supabase.table('pending_graph_nodes').select('status').eq('id', node_id).maybe_single().execute()
            if not current or not current.data or current.data.get('status') != 'pending':
                results["failed"] += 1
                results["details"].append(f"g{node_id} failed: no longer pending")
                continue
        except Exception:
            results["failed"] += 1
            results["details"].append(f"g{node_id} failed: DB error checking status")
            continue

        try:
            if action_type == 'reject':
                reason = action.get('reason', '')
                # Update status
                supabase.table('pending_graph_nodes').update({'status': 'rejected'}).eq('id', node_id).execute()
                
                # Audit log
                audit_log_sync("webhook", "INFO", f"Graph NLP: Rejected g{node_id} '{original['label']}'. Reason: {reason}")
                
                results["applied"] += 1
                results["details"].append(f"✅ g{node_id} rejected")
                
            elif action_type == 'approve':
                # Use corrected values or fall back to original
                final_label = action.get('corrected_label')
                if final_label is None or final_label.strip() == '':
                    final_label = original['label']
                    
                final_type = action.get('corrected_type')
                if final_type is None or final_type.strip() == '':
                    final_type = original['type']
                
                source_text = original.get('source_text', '')
                
                # Use shared helper to create DB record + graph_node + Danny edge
                result = await create_graph_node_with_db_record(
                    label=final_label,
                    node_type=final_type,
                    source_text=source_text,
                    context=action.get('context'),
                    source_tag="pending_approval_nlp"
                )
                
                if result.get('success'):
                    # Update pending status
                    supabase.table('pending_graph_nodes').update({'status': 'approved'}).eq('id', node_id).execute()
                    
                    audit_log_sync("webhook", "INFO", f"Graph NLP: Approved g{node_id}. Label: '{original['label']}' -> '{final_label}'. Type: '{original['type']}' -> '{final_type}'")
                    results["applied"] += 1
                    results["details"].append(f"✅ g{node_id} approved as '{final_label}' ({final_type})")
                else:
                    audit_log_sync("webhook", "WARNING", f"Graph NLP: g{node_id} helper returned: {result.get('message', 'unknown error')}")
                    results["failed"] += 1
                    results["details"].append(f"❌ g{node_id} failed: {result.get('message', 'unknown error')}")
                
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"Graph NLP apply error for g{node_id}: {e}")
            results["failed"] += 1
            results["details"].append(f"❌ g{node_id} failed: {e}")
            
    return results

