"""
completion_handler.py
Owns the full lifecycle of COMPLETION dumps.
State machine: processing_completion -> awaiting_completion_match | completed | partially_synced
"""
from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import asyncio

from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import compute_expires_at
from core.webhook.utils import supabase
from core.retrieval.pipeline import schedule_index_memory
from core.services.db import version_memory_for_update
from core.decisions import record_decision
from datetime import datetime, timezone
from core.actions import ActionResult, accumulate_action

    # ── Constants ─────────────────────────────────────────────────────────────────
COMPLETION_MESSAGE_TYPE = "completion"
STATUS_PROCESSING       = "processing_completion"
STATUS_AWAITING         = "awaiting_completion_match"
STATUS_COMPLETED        = "completed"
STATUS_PARTIAL          = "partially_synced"


async def handle_confident_completion(
    text: str,
    title: str,
    chat_id: int,
    receipt: str = None,
    entity: str = None,
    source: str = "telegram",
    sender: str = "user",
):
    dump_id = None
    try:
        # ── Stage 1: Insert with canonical message_type immediately ──────────
        # message_type='completion' is set at insert — this is the ownership flag.
        # All downstream logic predicates on this column, not transient metadata.
        dump_res = supabase.table("raw_dumps").insert({
            "content":      text,
            "status":       STATUS_PROCESSING,
            "is_processed": False,
            "direction":    "incoming",
            "sender":       sender,
            "source":       source,
            "message_type": COMPLETION_MESSAGE_TYPE,      # ← canonical flag
            "metadata": {
                "intent": "COMPLETION",
                "title":  title,
                "entity": entity,
            },
        }).execute()
        dump_id = dump_res.data[0]["id"] if dump_res.data else None

        # ── Stage 2: Embed + write to memories immediately (zero data loss) ──
        embedding = (await get_embedding(text)).vector
        embed_valid = bool(embedding and any(embedding))
        memory_id = None
        try:
            mem_res = supabase.table("memories").insert({
                "content":          text,
                "memory_type":      "note",
                "embedding":        embedding if embed_valid else None,
                "embedding_status": "success" if embed_valid else "failed",
                "source":           "webhook_completion",
                "metadata": {
                    "intent": "COMPLETION",
                    "entity": entity,
                },
                "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
            }).execute()
            memory_id = mem_res.data[0]['id'] if mem_res.data else None
            accumulate_action(ActionResult(action_type="memory_save", status="executed", entity_id=memory_id, human_label="Completion logged"))
            schedule_index_memory(memory_id, text, "note", "webhook_completion")
        except Exception as mem_err:
            audit_log_sync("completion", "WARNING", f"Memory write failed for dump {dump_id}: {mem_err}")
            pass

        # ── Stage 3: Deterministic narrowing — fetch live active tasks ────────
        tasks_res = supabase.table("tasks") \
            .select("id, title") \
            .eq("is_current", True) \
            .not_.in_("status", ["done", "cancelled"]) \
            .execute()
        active_tasks = tasks_res.data or []

        if not active_tasks:
            _park(dump_id, STATUS_AWAITING, "no_active_tasks")
            await _send(chat_id, "Completion logged. No open tasks to match right now.")
            return

        # Lexical prefilter — reduce LLM context to plausible candidates
        title_lower = title.lower()
        candidates = [
            t for t in active_tasks
            if any(word in t["title"].lower() for word in title_lower.split() if len(word) > 3)
        ]

        # ── Stage 4: Strict LLM matcher ───────────────────────────────────────
        from core.llm.fallback import generate_content_with_fallback
        from core.llm.config import WorkloadProfile
        from core.llm.constants import SYNTHESIS_MODEL
        candidate_lines = "\n".join(f"ID {t['id']}: {t['title']}" for t in candidates)
        match_prompt = f"""You are a task-matching engine. Given the completion message and a list of open tasks,
return ONLY a JSON object with one key: matched_task_ids (array of integers).
Return an empty array if nothing is a confident match. Do not explain.

Completion message: "{text}"
Extracted title: "{title}"

Open tasks:
{candidate_lines}

Rules:
- Only return IDs that genuinely correspond to the task being completed.
- Do not guess. An empty array is correct if unsure.
- IDs must come exactly from the list above.

Response: {{"matched_task_ids": [...]}}"""

        raw_ids = []
        for attempt_model in (CLASSIFICATION_MODEL, SYNTHESIS_MODEL):
            try:
                match_res = await generate_content_with_fallback(
                    prompt=match_prompt,
                    workload=WorkloadProfile.INTERACTIVE,
                    primary_model=attempt_model,
                    config={"response_mime_type": "application/json"},
                )
                parsed = match_res.parse_json()
                raw_ids = parsed.get("matched_task_ids", [])
                if raw_ids:
                    break
            except Exception as match_err:
                audit_log_sync("completion", "WARNING", f"LLM matcher failed (model={attempt_model}) for dump {dump_id}: {match_err}")

        # Strict validation — only IDs present in the active set are allowed
        active_id_set   = {t["id"] for t in active_tasks}
        validated_ids   = [i for i in raw_ids if i in active_id_set]

        if not validated_ids:
            # NO MATCH. Degrade to PROJECT_UPDATE instead of asking user.
            audit_log_sync("completion", "INFO", f"Override: Strict completion matcher found no task for '{title}'. Degraded dump {dump_id} to PROJECT_UPDATE.")
            
            # 1. Update raw_dump
            supabase.table("raw_dumps").update({
                "status": "processed", 
                "is_processed": True,
                "message_type": "note",
                "metadata": {
                    "intent": "PROJECT_UPDATE",
                    "title": title,
                    "entity": entity,
                    "degraded_from_completion": True
                }
            }).eq("id", dump_id).execute()
            
            # 2. Update memory if it was created (with versioning)
            if memory_id:
                update_data = version_memory_for_update(memory_id, {
                    "metadata": {
                        "intent": "PROJECT_UPDATE",
                        "entity": entity,
                        "degraded_from_completion": True
                    }
                })
                supabase.table("memories").update(update_data).eq("id", memory_id).execute()
                
                # 3. Extract and link entities
                try:
                    from core.pulse.entity_extractor import extract_and_link_entities
                    extracted = await extract_and_link_entities(text, memory_id, 'memory')
                    org_candidates, proj_candidates = extracted if extracted else ([], [])
                    
                    chosen_org_id = None
                    chosen_proj_id = None
                    if len(proj_candidates) == 1:
                        chosen_proj_id = proj_candidates[0]['id']
                        if proj_candidates[0].get('org_id'):
                            chosen_org_id = proj_candidates[0]['org_id']
                        elif len(org_candidates) == 1:
                            chosen_org_id = org_candidates[0]
                    elif len(org_candidates) == 1:
                        chosen_org_id = org_candidates[0]
                    
                    if chosen_org_id or chosen_proj_id:
                        update_data = {}
                        if chosen_org_id:
                            update_data['organization_id'] = chosen_org_id
                        if chosen_proj_id:
                            update_data['project_id'] = chosen_proj_id
                        update_data = version_memory_for_update(memory_id, update_data)
                        supabase.table('memories').update(update_data).eq('id', memory_id).execute()
                except Exception as e:
                    audit_log_sync("completion", "WARNING", f"Entity extraction failed for degraded update: {e}")
            
            # 4. Post-capture enrichment (shared helper)
            try:
                from core.webhook.dispatch import _run_post_capture_enrichment
                chosen_proj_id = locals().get('chosen_proj_id')
                chosen_org_id = locals().get('chosen_org_id')
                followup_msg = await _run_post_capture_enrichment(
                    text, chat_id, None,
                    chosen_org_id, chosen_proj_id,
                    receipt=receipt, enable_workflow=False,
                )
                ack = receipt or "✅ Logged as update (no matching task found)."
                await _send(chat_id, f"{ack}{followup_msg}")
            except Exception as e:
                audit_log_sync("completion", "WARNING", f"Update enrichment failed: {e}")
                ack = receipt or "✅ Logged as update (no matching task found)."
                await _send(chat_id, f"{ack}")

            return

        await execute_completion_closure(dump_id, validated_ids, chat_id, receipt, entity, active_tasks)

    except Exception as e:
        audit_log_sync("completion", "ERROR", f"handle_confident_completion fatal: {e}")
        if dump_id:
            _park(dump_id, STATUS_AWAITING, f"fatal_error: {str(e)[:100]}")
        await _send(chat_id, "Completion received. Had trouble matching — parked for review.")

async def execute_completion_closure(dump_id: int, validated_ids: list, chat_id: int, receipt: str, entity: str, active_tasks: list):
    """Extracted logic for idempotent closure to allow reuse by disambiguation resolver."""
    from core.pulse.memory import write_outcome_memory
    closed_ids      = []

    audit_log_sync("completion", "INFO", f"execute_completion_closure start: validated_ids={validated_ids}, dump_id={dump_id}")

    from core.pulse.tools import update_task_status
    sync_failed = False
    failed_tasks = []

    for task_id in validated_ids:
        row_res = supabase.table("tasks").select("id, title, status, google_task_id, google_event_id").eq("id", task_id).maybe_single().execute()
        row = row_res.data
        if not row:
            audit_log_sync("completion", "WARNING", f"Task {task_id} not found in DB — skipping.")
            continue
        if row["status"] in ("done", "cancelled"):
            audit_log_sync("completion", "INFO", f"Task {task_id} already terminal — skipping.")
            continue

        try:
            result_msg = update_task_status(task_id=task_id, status="done")
            audit_log_sync("completion", "INFO", f"update_task_status({task_id}) returned: {result_msg[:200]}")
            if "Error" in result_msg:
                audit_log_sync("completion", "ERROR", f"Tool failed for task {task_id}: {result_msg}")
                sync_failed = True
                failed_tasks.append(f"Task {task_id} ({row['title']}): {result_msg}")
            else:
                closed_ids.append(task_id)
                asyncio.create_task(write_outcome_memory(row["title"], entity))
        except Exception as close_err:
            audit_log_sync("completion", "ERROR", f"update_task_status tool failed for task {task_id}: {close_err}")
            sync_failed = True
            failed_tasks.append(f"Task {task_id} ({row['title']}): {close_err}")

    if not closed_ids and not sync_failed:
        _park(dump_id, STATUS_AWAITING, "already_closed")
        await _send(chat_id, "Completion noted — those tasks were already closed.")
        audit_log_sync("completion", "INFO", f"execute_completion_closure: no tasks closed. validated_ids={validated_ids}")
        return

    if sync_failed:
        _park(dump_id, STATUS_PARTIAL, "sync_failed")
        audit_log_sync("completion", "WARNING", f"execute_completion_closure: partial sync failure. closed_ids={closed_ids}")
        error_details = "\\n".join(failed_tasks)
        await _send(chat_id, f"⚠️ **Partial Sync Failure**\\nSome tasks couldn't be closed due to an external error. Please check manually:\\n\\n{error_details}")
    else:
        _park(dump_id, STATUS_COMPLETED, None, is_processed=True)
        audit_log_sync("completion", "INFO", f"execute_completion_closure: success. closed_ids={closed_ids}")

    if closed_ids:
        # Record a decision for each completed task
        try:
            for task_id in closed_ids:
                task_title = next((t["title"] for t in active_tasks if t["id"] == task_id), f"Task #{task_id}")
                record_decision(
                    decision_type="task_completion",
                    title=f"Completed: {task_title}",
                    context=entity or "completion_handler",
                    entity_type="task",
                    entity_id=str(task_id),
                    confidence=1.0,
                    source="completion_handler",
                )
        except Exception as dec_err:
            audit_log_sync("completion", "WARNING", f"Failed to record completion decision: {dec_err}")

        closed_titles = ", ".join(
            t["title"] for t in active_tasks if t["id"] in closed_ids
        )
        await _send(chat_id, f"✅ Closed: {closed_titles}")

ORDINALS = {
    'first': 0, '1st': 0, 'second': 1, '2nd': 1, 'third': 2, '3rd': 2,
    'fourth': 3, '4th': 3, 'fifth': 4, '5th': 4,
}
NULL_WORDS = {'n', 'none', 'skip', 'cancel', 'leave', 'neither', 'nothing', 'no',
              'leave it', 'none of these', 'none of them', 'dismiss'}
FILLER = {'the', 'a', 'an', 'it', 'is', 'was', 'this', 'that', 'one', 'to', 'for',
          'of', 'in', 'on', 'at', 'by', 'with', 'or', 'and', 'but', 'not', 'its',
          'about', 'just', 'do', 'done', 'task', 'please', 'thanks', 'my', 'me'}


async def _complete_candidate(candidate: dict, chat_id: int, last_clarification: dict) -> bool:
    """Select a candidate task and execute completion closure."""
    dump_id = last_clarification.get('dump_id')
    active_tasks = [{"id": candidate["id"], "title": candidate["title"]}]
    await execute_completion_closure(
        dump_id=dump_id,
        validated_ids=[candidate["id"]],
        chat_id=chat_id,
        receipt=None,
        entity=None,
        active_tasks=active_tasks
    )
    return True


async def resolve_completion_disambiguation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    """Handles the user's digit or natural reply for an ambiguous completion."""
    cleaned = text.strip().lower()
    candidates = last_clarification.get('candidate_tasks', [])
    
    if cleaned in NULL_WORDS:
        dump_id = last_clarification.get('dump_id')
        _park(dump_id, STATUS_AWAITING, "user_selected_none")
        await _send(chat_id, "Understood. Left on the board. Note vaulted.")
        return True
    
    if cleaned.isdigit():
        idx = int(cleaned) - 1
        if 0 <= idx < len(candidates):
            return await _complete_candidate(candidates[idx], chat_id, last_clarification)
    
    words = cleaned.split()
    for w in words:
        if w in ORDINALS:
            idx = ORDINALS[w]
            if 0 <= idx < len(candidates):
                return await _complete_candidate(candidates[idx], chat_id, last_clarification)
    
    query_words = [w for w in words if len(w) > 2 and w not in FILLER]
    if query_words:
        best_score = 0
        best_candidate = None
        for c in candidates:
            title_lower = c['title'].lower()
            score = sum(1 for qw in query_words if qw in title_lower)
            if score > best_score:
                best_score = score
                best_candidate = c
        if best_score >= 1 and best_candidate:
            return await _complete_candidate(best_candidate, chat_id, last_clarification)
    
    return False

def _park(dump_id, status, reason=None, is_processed=False):
    """Single update point for all dump state transitions."""
    if not dump_id:
        return
    update = {"status": status, "is_processed": is_processed}
    if reason:
        # Append to metadata non-destructively
        try:
            existing = supabase.table("raw_dumps").select("metadata").eq("id", dump_id).maybe_single().execute()
            meta = (existing.data or {}).get("metadata") or {}
            meta["park_reason"] = reason
            update["metadata"] = meta
        except Exception:
            pass  # Safe to skip — status update still proceeds
    supabase.table("raw_dumps").update(update).eq("id", dump_id).execute()


async def _send(chat_id, message):
    from core.webhook.telegram import send_telegram
    await send_telegram(chat_id, message)
