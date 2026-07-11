from core.llm import get_embedding
import json
import re
import asyncio
import hashlib
from datetime import datetime, timezone

from core.lib.audit_logger import info, audit_log_sync
from core.lib.process_input import ProcessInput, InvalidInput, normalize_and_validate
from core.services.db import get_supabase, fetch_active_projects, zombie_recovery, maybe_single_safe
from core.services.google_service import format_rfc3339, sync_to_calendar, sync_to_google, delete_calendar_event, get_tasks_service
from core.webhook.classify import CLASSIFICATION_MODEL
from core.actions import ActionResult, accumulate_action
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


# ── Subroutines ──────────────────────────────────────────────────────


async def save_url_as_resource(text: str) -> bool:
    match = re.search(r'https?://\S+', text)
    if not match:
        return False
    actual_url = match.group(0).rstrip('.,;:!?)"\'')
    try:
        existing = supabase.table('resources').select('id, dismissed_at').eq('url', actual_url).limit(1).execute()
        if not existing.data:
            supabase.table('resources').insert({"url": actual_url}).execute()
        elif existing.data[0].get('dismissed_at'):
            audit_log_sync("quick_process", "INFO", f"Skipped URL resource creation (dismissed): {actual_url}")
        return True
    except Exception as e:
        audit_log_sync("quick_process", "WARNING", f"Resource insert failed for URL: {e}")
        return False


async def _persist_note(text: str, input: ProcessInput, llm_result: dict | None = None) -> dict:
    """Save a NOTE to memories, extract entities, schedule index."""
    embedding_res = await get_embedding(text)
    embedding = embedding_res.vector if embedding_res else None
    try:
        record = {
            "content": text,
            "memory_type": input.memory_type,
            "embedding": embedding,
            "source": input.source,
        }
        if llm_result:
            record["sentiment_score"] = llm_result.get("sentiment_score")
            record["sentiment"] = llm_result.get("sentiment")
            record["entities_mentioned"] = llm_result.get("entities_mentioned") or []
            record["expires_at"] = compute_expires_at(text, datetime.now(timezone.utc).isoformat())
        elif input.expires_at:
            record["expires_at"] = input.expires_at

        res = supabase.table('memories').insert(record).execute()
        memory_id = res.data[0]['id'] if res.data else None
        if memory_id:
            accumulate_action(ActionResult(action_type="memory_save", status="executed", entity_id=memory_id, human_label="Note captured"))
            await extract_and_link_entities(text, memory_id, 'memory')
            schedule_index_memory(memory_id, text, input.memory_type, input.source)
        return {"action": "filed", "type": "note", "memory_id": memory_id}
    except Exception as e:
        audit_log_sync("quick_process", "WARNING", f"Memory insert failed: {e}")
        return {"action": "error", "reason": str(e)}


async def _persist_task(input: ProcessInput, tasks_service=None) -> dict:
    """Fresh task: dedup check, DB insert, sync, graph edges, entity extraction."""
    project_id, org_id = _resolve_project_and_org(input.project_name)

    dedup_key = hashlib_md5(f"{input.title.lower().strip()}:{project_id or 0}".encode())[:16]

    existing = supabase.table('tasks').select('id') \
        .eq('is_current', True) \
        .eq('dedup_key', dedup_key) \
        .not_.in_('status', ['done', 'cancelled']) \
        .limit(1).execute()
    if existing.data:
        return {"action": "skipped", "reason": "duplicate", "task_id": existing.data[0]['id']}

    task_insert = {
        "title": input.title,
        "project_id": project_id,
        "organization_id": org_id,
        "priority": input.priority,
        "status": "todo",
        "estimated_minutes": input.duration_mins,
        "duration_mins": input.duration_mins,
        "reminder_at": input.reminder_at,
        "dedup_key": dedup_key,
        "direction": input.direction,
        "committed_to": input.committed_to,
        "recurrence": input.recurrence,
    }

    try:
        insert_res = supabase.table('tasks').insert(task_insert).execute()
        task_id = insert_res.data[0]['id']
        accumulate_action(ActionResult(action_type="task_create", status="executed", entity_id=task_id, human_label=input.title))
    except Exception as e:
        audit_log_sync("quick_process", "ERROR", f"Task insert failed: {e}")
        accumulate_action(ActionResult(action_type="task_create", status="failed", evidence={"error": str(e)}))
        return {"action": "error", "reason": str(e)}

    result = await _run_task_syncs(input, task_id, tasks_service)

    try:
        t1 = write_graph_edges_for_task(task_id, input.title, project_id)
        t2 = extract_and_link_entities(input.text, task_id, 'task')
        await asyncio.gather(t1, t2, return_exceptions=True)
    except Exception as ge:
        audit_log_sync("quick_process", "WARNING", f"Failed to run graph edge tasks: {ge}")

    ret = {
        "action": "created",
        "task_id": task_id,
        "google_event_id": result.get("e_id"),
        "google_task_id": result.get("g_id"),
    }
    if result.get("conflict_warning"):
        ret["conflict_warning"] = result["conflict_warning"]
    return ret


async def _run_task_syncs(input: ProcessInput, task_id: int, tasks_service=None,
                          existing_e_id=None, existing_g_id=None,
                          existing_title=None, existing_priority=None,
                          check_conflicts=False) -> dict:
    """Synchronise a task's schedule with Google Calendar and Google Tasks.

    Shared by fresh task creation and task-update paths.
    Returns dict {e_id, g_id, conflict_warning}.
    """
    e_id = existing_e_id
    g_id = existing_g_id
    conflict_warning = None
    title = existing_title or input.title
    priority = existing_priority or input.priority
    sanitized_time = input.reminder_at
    explicit_time = bool(sanitized_time and 'T' in sanitized_time)

    if sanitized_time and explicit_time:
        if check_conflicts:
            try:
                conflict_name = await asyncio.to_thread(check_conflict, sanitized_time, e_id)
                if conflict_name:
                    conflict_warning = conflict_name
            except Exception as ce:
                audit_log_sync("quick_process", "WARNING", f"Calendar conflict check failed: {ce}")

        try:
            e_id = sync_to_calendar(title, sanitized_time, event_id=e_id, duration_mins=input.duration_mins, priority=priority, recurrence=input.recurrence)
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Calendar sync failed: {e}")
    elif e_id:
        try:
            delete_calendar_event(e_id)
            e_id = None
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Calendar delete failed: {e}")

    g_id_due_at = sanitized_time if sanitized_time else None
    if g_id and tasks_service:
        try:
            sync_to_google(tasks_service, title=title, task_id=g_id, status='todo', due_at=g_id_due_at)
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Google Tasks sync failed: {e}")
    elif tasks_service and not g_id and g_id_due_at:
        try:
            g_id = sync_to_google(tasks_service, title, g_id_due_at, explicit_time=explicit_time)
        except Exception as e:
            audit_log_sync("quick_process", "ERROR", f"Google Tasks create failed: {e}")

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

    return {"e_id": e_id, "g_id": g_id, "conflict_warning": conflict_warning}


def _resolve_project_and_org(project_name: str | None):
    """Resolve project_name to (project_id, org_id). Returns (None, None) if no match."""
    if not project_name:
        return None, None
    projects = fetch_active_projects()
    for p in projects:
        if p['name'].lower() == project_name.lower():
            return p['id'], None
    from core.features import is_org_routing_enabled
    if is_org_routing_enabled():
        try:
            org_res = supabase.table('organizations').select('id').ilike('name', project_name).limit(1).execute()
            if org_res.data:
                return None, org_res.data[0]['id']
            supabase.table('project_creation_signals').insert({
                "project_name": project_name,
                "source": "quick_process"
            }).execute()
        except Exception as e:
            audit_log_sync("quick_process", "WARNING", f"Org match failed: {e}")
    return None, None


# ── Main entry point ─────────────────────────────────────────────────


async def process_single_dump(text: str, metadata: dict, tasks_service=None,
                              history_text: str = "",
                              input: ProcessInput | None = None) -> dict:
    """Process a single text dump through classification, extraction, and persistence.

    When ``input`` is provided (pre-decided path), the LLM re-extraction is
    skipped and the typed ``ProcessInput`` is used directly. This is the
    canonical path for callers that already know the category (pulse engine,
    workflow confirms, ingests, enrichment).

    When ``input`` is None (legacy path), the LLM classifies and extracts
    structured data from the raw text before persisting.
    """
    # ── Pre-decided path ──
    if input is not None:
        try:
            input = normalize_and_validate(input)
        except InvalidInput as e:
            return {"action": "error", "reason": str(e)}

        if input.category == "RESOURCE":
            await save_url_as_resource(input.url or input.text)
            return {"action": "filed", "type": "resource"}

        if input.category == "NOTE":
            return await _persist_note(input.text, input)

        if input.category == "TASK":
            return await _persist_task(input, tasks_service)

    # ── Legacy LLM re-extraction path ──
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
    from core.prompts.ingest import build_quick_process_prompt
    prompt = build_quick_process_prompt(text, projects, history_text)

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

    if category in ('NOTE', 'PROJECT_UPDATE'):
        if re.search(r'https?://', text):
            await save_url_as_resource(text)
            return {"action": "filed", "type": "resource"}
        legacy_input = ProcessInput(category="NOTE", text=text, source="quick_process")
        return await _persist_note(text, legacy_input, llm_result=result)

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

    task_update_id = metadata.get('task_update_id')
    if task_update_id:
        task_ref = maybe_single_safe(
            supabase.table('tasks').select('id, google_task_id, google_event_id, title, status, priority')
            .eq('id', task_update_id)
            .eq('is_current', True)
        )
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

    active_tasks_res = supabase.table('tasks').select('id, title, status, google_event_id, google_task_id, priority') \
        .eq('is_current', True) \
        .not_.in_('status', ['done', 'cancelled']) \
        .execute()
    active_tasks = active_tasks_res.data or []

    guard = check_duplicate(title, active_tasks)

    dedup_key = hashlib_md5(f"{title.lower().strip()}:{project_id or 0}".encode())[:16]
    matched_id = None

    if guard['result'] == 'block':
        matched_id = guard['matched_id']

    if category == 'COMPLETION':
        if not matched_id:
            task_ref = maybe_single_safe(supabase.table('tasks').select('id').eq('dedup_key', dedup_key).eq('is_current', True))
            if task_ref.data:
                matched_id = task_ref.data['id']

        if matched_id:
            task_ref = maybe_single_safe(
                supabase.table('tasks').select('id, google_task_id, google_event_id, title, status')
                .eq('id', matched_id)
                .eq('is_current', True)
            )
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

                try:
                    await write_outcome_memory(td['title'], project_name)
                except Exception as oe:
                    audit_log_sync("quick_process", "WARNING", f"Failed to write outcome memory: {oe}")

                return {"action": "completed", "task_id": td['id']}
        return {"action": "skipped", "reason": "no_matching_task"}

    elif matched_id:
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

    existing = supabase.table('tasks').select('id') \
        .eq('is_current', True) \
        .eq('dedup_key', dedup_key) \
        .not_.in_('status', ['done', 'cancelled']) \
        .limit(1).execute()
    if existing.data:
        return {"action": "skipped", "reason": "duplicate", "task_id": existing.data[0]['id']}

    legacy_input = ProcessInput(
        category="TASK",
        text=text,
        source="quick_process",
        title=title,
        reminder_at=sanitized_time,
        priority=(result.get('priority') or 'important').lower(),
        duration_mins=result.get('duration_mins', 15),
        recurrence=result.get('recurrence'),
        direction=result.get("direction", "inbound"),
        committed_to=result.get("committed_to"),
    )
    return await _persist_task(legacy_input, tasks_service)


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
