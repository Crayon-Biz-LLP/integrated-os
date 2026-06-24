from core.llm import get_embedding
import json
import re
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from core.lib.audit_logger import info, audit_log_sync
from core.services.db import get_supabase,  fetch_active_projects, zombie_recovery
from core.services.google_service import format_rfc3339, sync_to_calendar, sync_to_google, delete_calendar_event, get_tasks_service
from core.webhook.classify import CLASSIFICATION_MODEL
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.lib.time_utils import compute_expires_at
from core.lib.duplicate_guard import check_duplicate
from core.pulse.calendar import check_conflict
from core.pulse.memory import write_outcome_memory
from core.pulse.graph import write_graph_edges_for_task
from core.pulse.entity_extractor import extract_and_link_entities
from core.retrieval.pipeline import schedule_index_memory

supabase = get_supabase()


async def save_url_as_resource(text: str) -> bool:
    match = re.search(r'https?://\S+', text)
    if not match:
        return False
    actual_url = match.group(0).rstrip('.,;:!?)"\'')
    try:
        supabase.table('resources').insert({"url": actual_url}).execute()
        return True
    except Exception as e:
        audit_log_sync("quick_process", "WARNING", f"Resource insert failed for URL: {e}")
        return False


def build_combined_prompt(text: str, projects: list, history_text: str = "") -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    date_context = now_ist.strftime("%A, %B %d, %Y at %I:%M %p IST")
    project_lines = "\n".join([
        f"  - {p['name']} (org: {p.get('organization_name', 'INBOX')})"
        for p in projects
    ]) if projects else "  - General (tag: INBOX)"

    return f"""You are Danny's task processor. Analyze this message.

Current date and time: {date_context}

{history_text}

Message: "{text}"

First, determine the category:
- TASK: An action item, something to do, a commitment, or a reschedule
- COMPLETION: Past tense — "finished", "done", "sorted", "confirmed", "sent", "wrapped up"
- NOTE: Idea, insight, observation (not actionable)
- NOISE: Casual conversation, acknowledgment, low-value content
- CLARIFY: If the user asks you to schedule a meeting or task but omits critical info (like time, date, or person) AND it cannot be inferred from the history, or if it is too vague. Generate a specific question in `clarification_question`.

Active projects for routing:
{project_lines}

If TASK or COMPLETION, extract these fields:
- title: Brief action-oriented title (2-8 words). If this is answering a clarification (e.g. "Tomorrow at 3pm"), merge the new detail with the original subject from the history into a complete title.
- project_name: Exact project name from the list above that best matches. Use "General" if none match.
- reminder_at: ISO-8601 datetime in IST (UTC+05:30) based on the current date above. If no time given, return null.
  Examples: "today 3pm" → "{now_ist.strftime('%Y-%m-%d')}T15:00:00+05:30"
            "tomorrow" → "{(now_ist + timedelta(days=1)).strftime('%Y-%m-%d')}"
            "next Friday 2pm" → "2026-05-22T14:00:00+05:30"
            "6:30 pm today" → "{now_ist.strftime('%Y-%m-%d')}T18:30:00+05:30"
- duration_mins: Estimated minutes (15 for quick tasks, 45 for meetings/calls)
- priority: "urgent", "important", or "low"
- recurrence: iCalendar RRULE string if recurring is mentioned (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO" or "RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20260831T000000Z"). Otherwise null.
- direction: "inbound" | "outbound" | "waiting_on" (default: inbound)
- committed_to: Person name if the task involves a commitment to or from someone

If NOTE, extract as structured fields if clear from the text:
- sentiment_score: -1.0 to 1.0 (null if unclear)
- sentiment: single word label (e.g., "frustrated", "grateful", "neutral")
- entities_mentioned: ["Marcus", "Equisoft"] (named entities only)

If COMPLETION: set status to "done"


STRICT RULES:
- If the message is ONLY a URL with no instruction, classify as NOTE
- Never create tasks from URLs unless there is a clear action instruction
- Never make up or hallucinate details not in the message

Return ONLY valid JSON:
{{
  "category": "TASK|COMPLETION|NOTE|NOISE|CLARIFY",
  "title": "...",
  "project_name": "...",
  "reminder_at": null,
  "recurrence": null,
  "duration_mins": 15,
  "priority": "important",
  "status": "todo",
  "clarification_question": "...",
  "direction": "inbound",
  "committed_to": null,
  "sentiment_score": null,
  "sentiment": null,
  "entities_mentioned": []
}}"""


async def process_single_dump(text: str, metadata: dict, tasks_service=None, history_text: str = "") -> dict:
    # ── Bypass: COMPLETION dumps are owned exclusively by handle_confident_completion.
    if (
        metadata.get("intent") == "COMPLETION"
        or metadata.get("message_type") == "completion"
    ):
        return {"action": "skipped", "reason": "completion_lane_exclusive"}

    stripped = text.strip()
    if re.match(r'^https?://\S+$', stripped):
        await save_url_as_resource(text)
        return {"action": "filed", "type": "resource"}

    projects = fetch_active_projects()
    prompt = build_combined_prompt(text, projects, history_text)

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=CLASSIFICATION_MODEL,
            is_classification=True,
            config={'response_mime_type': 'application/json'}
        )
        result = response.parse_json()
    except Exception as e:
        audit_log_sync("quick_process", "ERROR", f"AI call failed: {e}")
        return {"action": "error", "reason": str(e)}

    category = result.get('category', 'NOTE')

    if category == 'CLARIFY':
        return {"action": "clarify", "question": result.get('clarification_question', "I need a bit more detail to process this.")}

    if category == 'NOISE':
        return {"action": "skipped", "reason": "noise"}

    if category == 'NOTE':
        if re.search(r'https?://', text):
            await save_url_as_resource(text)
            return {"action": "filed", "type": "resource"}
            
        embedding_res = await get_embedding(text)
        embedding = embedding_res.vector if embedding_res else None
        try:
            ins_res = supabase.table('memories').insert({
                "content": text,
                "memory_type": "note",
                "embedding": embedding,
                "source": "quick_process",
                "sentiment_score": result.get("sentiment_score"),
                "sentiment": result.get("sentiment"),
                "entities_mentioned": result.get("entities_mentioned") or [],
                "expires_at": compute_expires_at(text, datetime.now(timezone.utc).isoformat())
            }).execute()
            memory_id = ins_res.data[0]['id']
        except Exception as e:
            audit_log_sync("quick_process", "WARNING", f"Memory insert failed: {e}")
            
        try:
            if memory_id:
                await extract_and_link_entities(text, memory_id, 'memory')
                schedule_index_memory(memory_id, text, 'note', 'quick_process')
        except Exception as e:
            audit_log_sync("quick_process", "WARNING", f"extract_and_link_entities failed: {e}")
            
        return {"action": "filed", "type": "note"}

    title = result.get('title', text[:80])
    project_name = result.get('project_name', '')
    project_id = None
    org_id = None
    
    if project_name:
        for p in projects:
            if p['name'].lower() == project_name.lower():
                project_id = p['id']
                break
                
        from core.features import is_org_routing_enabled
        if not project_id and is_org_routing_enabled():
            # Try to match org
            try:
                org_res = supabase.table('organizations').select('id').ilike('name', project_name).limit(1).execute()
                if org_res.data:
                    org_id = org_res.data[0]['id']
                else:
                    # Log signal for Pulse
                    supabase.table('project_creation_signals').insert({
                        "project_name": project_name,
                        "source": "quick_process"
                    }).execute()
            except Exception as e:
                audit_log_sync("quick_process", "WARNING", f"Org match failed: {e}")

    sanitized_time = format_rfc3339(result.get('reminder_at'))
    explicit_time = bool(result.get('reminder_at') and 'T' in str(result.get('reminder_at')))

    task_update_id = metadata.get('task_update_id')
    if task_update_id:
        task_ref = supabase.table('tasks').select('id, google_task_id, google_event_id, title, status, priority') \
            .eq('id', task_update_id) \
            .eq('is_current', True) \
            .maybe_single().execute()
        if task_ref.data:
            td = task_ref.data
            e_id = td.get('google_event_id')
            g_id = td.get('google_task_id')
            
            update_payload = {}
            if result.get('duration_mins'):
                update_payload["duration_mins"] = result.get('duration_mins')
                update_payload["estimated_minutes"] = result.get('duration_mins')
            if result.get('direction'):
                update_payload["direction"] = result.get('direction')
            if result.get('committed_to'):
                update_payload["committed_to"] = result.get('committed_to')
                
            if sanitized_time:
                update_payload["reminder_at"] = sanitized_time
                
                if explicit_time:
                    try:
                        e_id = sync_to_calendar(td['title'], sanitized_time, event_id=e_id, duration_mins=result.get('duration_mins', 15), priority=td.get('priority', 'important'), recurrence=result.get('recurrence'))
                        update_payload['google_event_id'] = e_id
                    except Exception as e:
                        audit_log_sync("quick_process", "ERROR", f"Calendar sync failed on update: {e}")
                elif e_id:
                    delete_calendar_event(e_id)
                    update_payload['google_event_id'] = None
                
                if g_id and tasks_service:
                    try:
                        sync_to_google(tasks_service, title=td['title'], task_id=g_id, status=td['status'], due_at=sanitized_time)
                    except Exception as e:
                        audit_log_sync("quick_process", "ERROR", f"Google Tasks sync failed on update: {e}")

            if update_payload:
                supabase.table('tasks').update(update_payload).eq('id', task_update_id).execute()
                
            return {"action": "updated", "task_id": task_update_id}

    # 🛡️ 1. Fetch active tasks for Semantic Guard
    active_tasks_res = supabase.table('tasks').select('id, title, status, google_event_id, google_task_id, priority') \
        .eq('is_current', True) \
        .not_.in_('status', ['done', 'cancelled']) \
        .execute()
    active_tasks = active_tasks_res.data or []
    
    # 🛡️ 2. Run Semantic Guard
    guard = check_duplicate(title, active_tasks)
    
    dedup_key = hashlib_md5(f"{title.lower().strip()}:{project_id or 0}".encode())[:16]
    matched_id = None
    
    if guard['result'] == 'block':
        matched_id = guard['matched_id']
    
    if category == 'COMPLETION':
        if not matched_id:
            # Fallback to MD5 dedup_key if semantic guard missed it
            task_ref = supabase.table('tasks').select('id').eq('dedup_key', dedup_key).eq('is_current', True).maybe_single().execute()
            if task_ref.data:
                matched_id = task_ref.data['id']
                
        if matched_id:
            task_ref = supabase.table('tasks').select('id, google_task_id, google_event_id, title, status') \
                .eq('id', matched_id) \
                .eq('is_current', True) \
                .maybe_single().execute()
            if task_ref.data and task_ref.data['status'] not in ('done', 'cancelled'):
                td = task_ref.data
                if td.get('google_event_id'):
                    delete_calendar_event(td['google_event_id'])
                if td.get('google_task_id') and tasks_service:
                    sync_to_google(tasks_service, title=td['title'], task_id=td['google_task_id'], status='done')
                supabase.table('tasks').update({
                    "status": "done",
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq('id', td['id']).execute()
                
                # 🧠 Write outcome memory
                try:
                    await write_outcome_memory(td['title'], project_name)
                except Exception as oe:
                    audit_log_sync("quick_process", "WARNING", f"Failed to write outcome memory: {oe}")
                
                return {"action": "completed", "task_id": td['id']}
        return {"action": "skipped", "reason": "no_matching_task"}
        
    elif matched_id:
        # Semantic guard matched an existing task -> treat as UPDATE instead of duplicate skip
        matched_task = next((t for t in active_tasks if t['id'] == matched_id), None)
        if matched_task:
            td = matched_task
            e_id = td.get('google_event_id')
            g_id = td.get('google_task_id')
            
            update_payload = {}
            if result.get('duration_mins'):
                update_payload["duration_mins"] = result.get('duration_mins')
                update_payload["estimated_minutes"] = result.get('duration_mins')
            if result.get('direction'):
                update_payload["direction"] = result.get('direction')
            if result.get('committed_to'):
                update_payload["committed_to"] = result.get('committed_to')
                
            conflict_warning = None
            if sanitized_time:
                update_payload["reminder_at"] = sanitized_time
                
                if explicit_time:
                    try:
                        # Conflict check
                        try:
                            conflict_name = await asyncio.to_thread(check_conflict, sanitized_time, e_id)
                            if conflict_name:
                                conflict_warning = conflict_name
                        except Exception as ce:
                            audit_log_sync("quick_process", "WARNING", f"Calendar conflict check failed: {ce}")
                            
                        e_id = sync_to_calendar(td['title'], sanitized_time, event_id=e_id, duration_mins=result.get('duration_mins', 15), priority=td.get('priority', 'important'), recurrence=result.get('recurrence'))
                        update_payload['google_event_id'] = e_id
                    except Exception as e:
                        audit_log_sync("quick_process", "ERROR", f"Calendar sync failed on update: {e}")
                elif e_id:
                    delete_calendar_event(e_id)
                    update_payload['google_event_id'] = None
                
                if g_id and tasks_service:
                    try:
                        sync_to_google(tasks_service, title=td['title'], task_id=g_id, status=td.get('status', 'todo'), due_at=sanitized_time)
                    except Exception as e:
                        audit_log_sync("quick_process", "ERROR", f"Google Tasks sync failed on update: {e}")

            if update_payload:
                supabase.table('tasks').update(update_payload).eq('id', matched_id).execute()
                
            ret = {"action": "updated", "task_id": matched_id}
            if conflict_warning:
                ret["conflict_warning"] = conflict_warning
            return ret
            
    # Fallback strict idempotency guard
    existing = supabase.table('tasks').select('id') \
        .eq('is_current', True) \
        .eq('dedup_key', dedup_key) \
        .not_.in_('status', ['done', 'cancelled']) \
        .limit(1).execute()
    if existing.data:
        return {"action": "skipped", "reason": "duplicate", "task_id": existing.data[0]['id']}

    task_insert = {
        "title": title,
        "project_id": project_id,
        "organization_id": org_id,
        "priority": (result.get('priority') or 'important').lower(),
        "status": "todo",
        "estimated_minutes": result.get('duration_mins', 15),
        "duration_mins": result.get('duration_mins', 15),
        "reminder_at": sanitized_time,
        "dedup_key": dedup_key,
        "direction": result.get("direction", "inbound"),
        "committed_to": result.get("committed_to")
    }

    try:
        insert_res = supabase.table('tasks').insert(task_insert).execute()
        task_id = insert_res.data[0]['id']
    except Exception as e:
        audit_log_sync("quick_process", "ERROR", f"Task insert failed: {e}")
        return {"action": "error", "reason": str(e)}

    e_id = None
    g_id = None
    conflict_warning = None

    if sanitized_time and explicit_time:
        try:
            try:
                conflict_name = await asyncio.to_thread(check_conflict, sanitized_time)
                if conflict_name:
                    conflict_warning = conflict_name
            except Exception as ce:
                audit_log_sync("quick_process", "WARNING", f"Calendar conflict check failed: {ce}")
                
            e_id = sync_to_calendar(title, sanitized_time, task_insert['duration_mins'], priority=task_insert['priority'], recurrence=result.get('recurrence'))
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Calendar sync failed: {e}")
    if sanitized_time and tasks_service:
        try:
            g_id = sync_to_google(tasks_service, title, sanitized_time, explicit_time=explicit_time)
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Google Tasks sync failed: {e}")

    if e_id or g_id:
        update = {}
        if e_id:
            update['google_event_id'] = e_id
        if g_id:
            update['google_task_id'] = g_id
        try:
            supabase.table('tasks').update(update).eq('id', task_id).execute()
        except Exception:
            pass
            
    # 🧠 Write graph edges for new task
    try:
        t1 = write_graph_edges_for_task(task_id, title, project_id)
        t2 = extract_and_link_entities(text, task_id, 'task')
        await asyncio.gather(t1, t2, return_exceptions=True)
    except Exception as ge:
        audit_log_sync("quick_process", "WARNING", f"Failed to run graph edge tasks: {ge}")

    ret = {"action": "created", "task_id": task_id, "google_event_id": e_id, "google_task_id": g_id}
    if conflict_warning:
        ret["conflict_warning"] = conflict_warning
    return ret


def hashlib_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


async def process_pending_dumps():
    zombie_recovery()

    dumps_res = supabase.table('raw_dumps') \
        .select('id, content, metadata, source') \
        .in_('status', ['pending', 'staged']) \
        .limit(20) \
        .execute()
    dumps = dumps_res.data or []
    if not dumps:
        return {"processed": 0}

    tasks_service = get_tasks_service()
    processed = 0
    for d in dumps:
        if d.get('message_type') not in ('task', None):
            continue
        meta = d.get('metadata') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        lock_res = supabase.table('raw_dumps') \
            .update({"status": "processing"}) \
            .eq('id', d['id']) \
            .in_('status', ['pending', 'staged']) \
            .execute()
        if not lock_res.data:
            continue

        result = await process_single_dump(d['content'], meta, tasks_service)

        if result.get('action') in ('created', 'completed', 'filed', 'updated'):
            supabase.table('raw_dumps').update({
                "status": "synced"
            }).eq('id', d['id']).execute()
            processed += 1
            audit_log_sync("quick_process", "INFO", f"Processed dump {d['id']}: {result['action']}")
        elif result.get('action') == 'error':
            supabase.table('raw_dumps').update({
                "status": "staged",
                "metadata": {**meta, "quick_process_error": result.get('reason')}
            }).eq('id', d['id']).execute()
        else:
            supabase.table('raw_dumps').update({
                "status": "completed",
                "is_processed": True
            }).eq('id', d['id']).execute()
            processed += 1

    return {"processed": processed}


async def main():
    import time
    start = time.time()
    info("quick_process", "Starting quick_process cycle")
    result = await process_pending_dumps()
    elapsed = time.time() - start
    info("quick_process", f"Cycle complete: {result['processed']} dumps in {elapsed:.1f}s")
    print(f"Quick process: {result['processed']} dumps in {elapsed:.1f}s")


if __name__ == '__main__':
    asyncio.run(main())
