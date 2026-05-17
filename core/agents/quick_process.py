import os
import json
import re
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from core.lib.audit_logger import info, warning, error, audit_log_sync
from core.lib.rate_limiter import flash_lite_limiter
from core.services.db import get_supabase, get_embedding, fetch_active_projects, zombie_recovery, versioned_update
from core.services.google_service import format_rfc3339, sync_to_calendar, sync_to_google, delete_calendar_event, get_tasks_service
from core.services.llm import call_gemini_classify, CLASSIFICATION_MODEL, get_gemini_client

supabase = get_supabase()


def is_bare_url(text: str) -> bool:
    stripped = text.strip()
    return bool(re.match(r'^https?://\S+$', stripped))


def build_combined_prompt(text: str, projects: list) -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    date_context = now_ist.strftime("%A, %B %d, %Y at %I:%M %p IST")
    project_lines = "\n".join([
        f"  - {p['name']} (tag: {p.get('org_tag', 'INBOX')})"
        for p in projects
    ]) if projects else "  - General (tag: INBOX)"

    return f"""You are Danny's task processor. Analyze this message.

Current date and time: {date_context}

Message: "{text}"

First, determine the category:
- TASK: An action item, something to do, a commitment, or a reschedule
- COMPLETION: Past tense — "finished", "done", "sorted", "confirmed", "sent", "wrapped up"
- NOTE: Idea, insight, observation (not actionable)
- NOISE: Casual conversation, acknowledgment, low-value content

Active projects for routing:
{project_lines}

If TASK or COMPLETION, extract these fields:
- title: Brief action-oriented title (2-8 words)
- project_name: Exact project name from the list above that best matches. Use "General" if none match.
- reminder_at: ISO-8601 datetime in IST (UTC+05:30) based on the current date above. If no time given, return null.
  Examples: "today 3pm" → "{now_ist.strftime('%Y-%m-%d')}T15:00:00+05:30"
            "tomorrow" → "{(now_ist + timedelta(days=1)).strftime('%Y-%m-%d')}"
            "next Friday 2pm" → "2026-05-22T14:00:00+05:30"
            "6:30 pm today" → "{now_ist.strftime('%Y-%m-%d')}T18:30:00+05:30"
- duration_mins: Estimated minutes (15 for quick tasks, 45 for meetings/calls)
- priority: "urgent", "important", or "low"

If COMPLETION: set status to "done"

STRICT RULES:
- If the message is ONLY a URL with no instruction, classify as NOTE
- Never create tasks from URLs unless there is a clear action instruction
- Never make up or hallucinate details not in the message

Return ONLY valid JSON:
{{
  "category": "TASK|COMPLETION|NOTE|NOISE",
  "title": "...",
  "project_name": "...",
  "reminder_at": null,
  "duration_mins": 15,
  "priority": "important",
  "status": "todo"
}}"""


async def process_single_dump(text: str, metadata: dict, tasks_service=None) -> dict:
    if is_bare_url(text):
        return {"action": "skipped", "reason": "bare_url"}

    projects = fetch_active_projects()
    prompt = build_combined_prompt(text, projects)

    try:
        response = await call_gemini_classify(
            prompt,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'application/json'}
        )
        result = json.loads(response.text.strip().replace('```json', '').replace('```', '').strip())
    except Exception as e:
        audit_log_sync("quick_process", "ERROR", f"AI call failed: {e}")
        return {"action": "error", "reason": str(e)}

    category = result.get('category', 'NOTE')

    if category == 'NOISE':
        return {"action": "skipped", "reason": "noise"}

    if category == 'NOTE':
        embedding = get_embedding(text)
        try:
            supabase.table('memories').insert({
                "content": text,
                "memory_type": "note",
                "embedding": embedding,
                "source": "quick_process"
            }).execute()
        except Exception as e:
            audit_log_sync("quick_process", "WARNING", f"Memory insert failed: {e}")
        return {"action": "filed", "type": "note"}

    title = result.get('title', text[:80])
    project_name = result.get('project_name', '')
    project_id = None
    if project_name:
        for p in projects:
            if p['name'].lower() == project_name.lower():
                project_id = p['id']
                break

    sanitized_time = format_rfc3339(result.get('reminder_at'))
    explicit_time = bool(result.get('reminder_at') and 'T' in str(result.get('reminder_at')))

    dedup_key = hashlib_md5(f"{title.lower().strip()}:{project_id or 0}".encode())[:16]
    existing = supabase.table('tasks').select('id') \
        .eq('dedup_key', dedup_key) \
        .not_.in_('status', ['done', 'cancelled']) \
        .limit(1).execute()
    if existing.data:
        return {"action": "skipped", "reason": "duplicate", "task_id": existing.data[0]['id']}

    if category == 'COMPLETION':
        task_ref = supabase.table('tasks').select('id, google_task_id, google_event_id, title, status') \
            .eq('dedup_key', dedup_key) \
            .eq('is_current', True) \
            .maybe_single().execute()
        if task_ref.data and task_ref.data['status'] not in ('done', 'cancelled'):
            td = task_ref.data
            if td.get('google_event_id'):
                delete_calendar_event(td['google_event_id'])
            if td.get('google_task_id') and tasks_service:
                sync_to_google(tasks_service, title=td['title'], task_id=td['google_task_id'], status='done')
            versioned_update('tasks', td['id'], {"status": "done", "completed_at": datetime.now(timezone.utc).isoformat()},
                             change_source='quick_process', change_reason="Completed via quick_process")
            return {"action": "completed", "task_id": td['id']}
        return {"action": "skipped", "reason": "no_matching_task"}

    task_insert = {
        "title": title,
        "project_id": project_id,
        "priority": (result.get('priority') or 'important').lower(),
        "status": "todo",
        "estimated_minutes": result.get('duration_mins', 15),
        "duration_mins": result.get('duration_mins', 15),
        "reminder_at": sanitized_time,
        "dedup_key": dedup_key,
    }

    try:
        insert_res = supabase.table('tasks').insert(task_insert).execute()
        task_id = insert_res.data[0]['id']
    except Exception as e:
        audit_log_sync("quick_process", "ERROR", f"Task insert failed: {e}")
        return {"action": "error", "reason": str(e)}

    e_id = None
    g_id = None

    if sanitized_time and explicit_time:
        try:
            e_id = sync_to_calendar(title, sanitized_time, task_insert['duration_mins'])
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

    return {"action": "created", "task_id": task_id, "google_event_id": e_id, "google_task_id": g_id}


def hashlib_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


async def process_pending_dumps():
    zombie_recovery()

    dumps_res = supabase.table('raw_dumps') \
        .select('id, content, metadata, message_type') \
        .eq('status', 'pending') \
        .execute()
    dumps = dumps_res.data or []
    if not dumps:
        return {"processed": 0}

    tasks_service = get_tasks_service()
    processed = 0
    for d in dumps:
        if d.get('message_type') not in ('task', None):
            continue
        meta = d.get('metadata', {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        lock_res = supabase.table('raw_dumps') \
            .update({"status": "processing"}) \
            .eq('id', d['id']) \
            .eq('status', 'pending') \
            .execute()
        if not lock_res.data:
            continue

        result = await process_single_dump(d['content'], meta, tasks_service)

        if result.get('action') in ('created', 'completed', 'filed'):
            supabase.table('raw_dumps').update({
                "status": "synced"
            }).eq('id', d['id']).execute()
            processed += 1
            audit_log_sync("quick_process", "INFO", f"Processed dump {d['id']}: {result['action']}")
        elif result.get('action') == 'error':
            supabase.table('raw_dumps').update({
                "status": "pending",
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
