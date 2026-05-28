"""
completion_handler.py
Owns the full lifecycle of COMPLETION dumps.
State machine: processing_completion -> awaiting_completion_match | completed | partially_synced
"""

import json
import asyncio
from datetime import datetime, timezone

from core.lib.audit_logger import audit_log_sync
from core.webhook.utils import supabase
from core.services.db import versioned_update

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
        from core.webhook.classify import get_embedding
        embedding = await asyncio.to_thread(get_embedding, text)
        embed_valid = bool(embedding and any(embedding))
        try:
            supabase.table("memories").insert({
                "content":          text,
                "memory_type":      "note",
                "embedding":        embedding if embed_valid else None,
                "embedding_status": "embedded" if embed_valid else "failed",
                "source":           "webhook_completion",
                "metadata": {
                    "intent": "COMPLETION",
                    "entity": entity,
                }
            }).execute()
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
        from core.webhook.classify import call_gemini_with_retry
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
            match_res = await call_gemini_with_retry(
                prompt=match_prompt,
                config={"response_mime_type": "application/json"},
            )
            parsed      = json.loads(match_res.text.strip().replace("```json", "").replace("```", ""))
            raw_ids     = parsed.get("matched_task_ids", [])
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
                opts = []
                for i, c in enumerate(candidates):
                    opts.append(f"{i+1}️⃣ — {c['title']}")
                opts.append("n — None of these (Leave open)")
                
                reply = "🧐 *Which task did you complete?*\n\n" + "\n".join(opts) + "\n\n_Reply with the number, or 'n' for none._"
                
                # Save clarification state to conversations
                from core.lib.conversation import get_or_create_session, log_exchange
                session_id, _ = get_or_create_session(chat_id)
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
                await _send(chat_id, reply)
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
    now_utc         = datetime.now(timezone.utc).isoformat()

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
            versioned_update("tasks", task_id, {
                "status":       "done",
                "completed_at": now_utc,
            }, change_source='webhook_completion', change_reason='Inline completion')
            closed_ids.append(task_id)
            # Write outcome memory per closed task
            asyncio.create_task(write_outcome_memory(row["title"], entity))
        except Exception as close_err:
            audit_log_sync("completion", "ERROR", f"versioned_update failed for task {task_id}: {close_err}")

    if not closed_ids:
        # Matched but all were already terminal
        _park(dump_id, STATUS_AWAITING, "already_closed")
        await _send(chat_id, receipt or "Completion noted — those tasks were already closed.")
        return

    # ── Stage 6: Isolated external sync ───────────────────────────────────
    # DB is already committed. Sync failure does NOT roll back.
    sync_failed = False
    try:
        from core.services.google_service import get_tasks_service, sync_to_google, delete_calendar_event
        tasks_service = get_tasks_service()
        for task_id in closed_ids:
            row_res = supabase.table("tasks").select("title, google_task_id, google_event_id").eq("id", task_id).maybe_single().execute()
            row = row_res.data
            if row:
                if row.get('google_event_id'):
                    try:
                        delete_calendar_event(row['google_event_id'])
                    except Exception as ce:
                        audit_log_sync("completion", "WARNING", f"Calendar delete failed for {task_id}: {ce}")
                if row.get('google_task_id') and tasks_service:
                    await asyncio.to_thread(sync_to_google, tasks_service, title=row['title'], task_id=row['google_task_id'], status="done")
    except Exception as sync_err:
        audit_log_sync("completion", "WARNING", f"External sync failed for dump {dump_id}: {sync_err}")
        sync_failed = True

    if sync_failed:
        from core.services.pipeline_service import add_to_failed_queue
        for task_id in closed_ids:
            await add_to_failed_queue("tasks", str(task_id), "google_sync", "Sync failed post-completion")
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
