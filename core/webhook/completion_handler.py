"""
completion_handler.py
Owns the full lifecycle of COMPLETION dumps.
State machine: processing_completion -> awaiting_completion_match | completed | partially_synced
"""
from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import json
import asyncio

from core.lib.audit_logger import audit_log_sync
from core.lib.time_utils import compute_expires_at
from core.webhook.utils import supabase
from core.retrieval.pipeline import schedule_index_memory
from datetime import datetime, timezone

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
            if mem_res and mem_res.data:
                schedule_index_memory(mem_res.data[0]["id"], text,
                                      "note", "webhook_completion")
        except Exception as mem_err:
            audit_log_sync("completion", "WARNING", f"Memory write failed for dump {dump_id}: {mem_err}")
            from core.services.pipeline_service import add_to_failed_queue
            await add_to_failed_queue('memories', str(dump_id), 'memory_insert', str(mem_err))

        # ── Stage 3: Deterministic narrowing — fetch live active tasks ────────
        tasks_res = supabase.table("tasks") \
            .select("id, title") \
            .eq("is_current", True) \
            .not_.in_("status", ["done", "cancelled"]) \
            .execute()
        active_tasks = tasks_res.data or []

        if not active_tasks:
            _park(dump_id, STATUS_AWAITING, "no_active_tasks")
            await _send(chat_id, receipt or "Completion logged. No open tasks to match right now.")
            return

        # Lexical prefilter — reduce LLM context to plausible candidates
        title_lower = title.lower()
        candidates = [
            t for t in active_tasks
            if any(word in t["title"].lower() for word in title_lower.split() if len(word) > 3)
        ] or active_tasks[:10]   # fallback: send top 10 if prefilter returns nothing

        # ── Stage 4: Strict LLM matcher ───────────────────────────────────────
        from core.llm.fallback import generate_content_with_fallback
        from core.llm.config import WorkloadProfile
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

        try:
            match_res = await generate_content_with_fallback(
                prompt=match_prompt,
                workload=WorkloadProfile.INTERACTIVE,
                primary_model=CLASSIFICATION_MODEL,
                config={"response_mime_type": "application/json"},
            )
            parsed = match_res.parse_json()
            raw_ids = parsed.get("matched_task_ids", [])
        except Exception as match_err:
            audit_log_sync("completion", "WARNING", f"LLM matcher failed for dump {dump_id}: {match_err}")
            raw_ids = []

        # Strict validation — only IDs present in the active set are allowed
        active_id_set   = {t["id"] for t in active_tasks}
        validated_ids   = [i for i in raw_ids if i in active_id_set]

        if not validated_ids:
            # Clarification Fallback (The Ambiguity Check)
            if candidates and len(candidates) <= 5:
                # Ask user to pick which task they meant
                reply = "🧐 *Which task did you complete?*"
                keyboard = []
                for i, c in enumerate(candidates):
                    # Button text max 64 chars. Callback data is just the digit string (e.g. "1")
                    title_short = c['title'][:50] + ("..." if len(c['title']) > 50 else "")
                    keyboard.append([{"text": f"{i+1}️⃣ {title_short}", "callback_data": str(i+1)}])
                keyboard.append([{"text": "None of these (Leave open)", "callback_data": "n"}])
                
                # Save clarification state to conversations
                from core.lib.conversation import get_or_create_session, log_exchange
                session_id, _, _ = get_or_create_session(chat_id)
                log_exchange(
                    session_id, 'bot', 'CLARIFICATION',
                    json.dumps({
                        "confirmation": "completion_disambiguation",
                        "dump_id": dump_id,
                        "candidate_tasks": candidates,
                        "original": text
                    }),
                    chat_id
                )
                
                _park(dump_id, STATUS_AWAITING, "awaiting_clarification")
                from core.webhook.telegram import send_telegram
                await send_telegram(chat_id, reply, show_keyboard=False, inline_keyboard=keyboard)
                return
            else:
                _park(dump_id, STATUS_AWAITING, "no_match")
                await _send(chat_id, receipt or "Completion logged. Couldn't match a specific task — parked for review.")
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

    from core.pulse.tools import update_task_status
    sync_failed = False

    for task_id in validated_ids:
        # Read current row — skip if already terminal
        row_res = supabase.table("tasks").select("id, title, status, google_task_id, google_event_id").eq("id", task_id).maybe_single().execute()
        row = row_res.data
        if not row:
            continue
        if row["status"] in ("done", "cancelled"):
            audit_log_sync("completion", "INFO", f"Task {task_id} already terminal — skipping.")
            continue

        try:
            # Use the canonical tool to do the DB update, Calendar delete, and Google Tasks sync
            result_msg = update_task_status(task_id=task_id, status="done")
            if "Error" in result_msg:
                audit_log_sync("completion", "ERROR", f"Tool failed for task {task_id}: {result_msg}")
                sync_failed = True
            else:
                closed_ids.append(task_id)
                # Write outcome memory per closed task
                asyncio.create_task(write_outcome_memory(row["title"], entity))
        except Exception as close_err:
            audit_log_sync("completion", "ERROR", f"update_task_status tool failed for task {task_id}: {close_err}")
            sync_failed = True

    if not closed_ids:
        # Matched but all were already terminal
        _park(dump_id, STATUS_AWAITING, "already_closed")
        await _send(chat_id, receipt or "Completion noted — those tasks were already closed.")
        return

    if sync_failed:
        from core.services.pipeline_service import add_to_failed_queue
        for task_id in closed_ids:
            await add_to_failed_queue("tasks", str(task_id), "google_sync", "Sync failed post-completion via tool")
        _park(dump_id, STATUS_PARTIAL, "sync_failed")
    else:
        # ── Only seal to completed if we can prove DB mutations occurred ──
        _park(dump_id, STATUS_COMPLETED, None, is_processed=True)

    closed_titles = ", ".join(
        t["title"] for t in active_tasks if t["id"] in closed_ids
    )
    await _send(chat_id, receipt or f"✅ Closed: {closed_titles}")

async def resolve_completion_disambiguation(text: str, chat_id: int, session_id: str, last_clarification: dict) -> bool:
    """Handles the user's digit reply for an ambiguous completion."""
    cleaned = text.strip().lower()
    
    if cleaned in ('n', 'none'):
        dump_id = last_clarification.get('dump_id')
        _park(dump_id, STATUS_AWAITING, "user_selected_none")
        await _send(chat_id, "Understood. Left on the board. Note vaulted.")
        return True
        
    if cleaned.isdigit():
        idx = int(cleaned) - 1
        candidates = last_clarification.get('candidate_tasks', [])
        
        if 0 <= idx < len(candidates):
            selected_task = candidates[idx]
            dump_id = last_clarification.get('dump_id')
            
            # Retrieve active tasks context (just for title mapping)
            active_tasks = [{"id": selected_task["id"], "title": selected_task["title"]}]
            
            await execute_completion_closure(
                dump_id=dump_id,
                validated_ids=[selected_task["id"]],
                chat_id=chat_id,
                receipt=None,
                entity=None,
                active_tasks=active_tasks
            )
            return True
            
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
