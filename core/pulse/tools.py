import asyncio
import hashlib
import json
from datetime import datetime, timezone

from core.retrieval.pipeline import schedule_index_memory
from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import sync_to_calendar, sync_to_google, get_tasks_service, delete_calendar_event, delete_calendar_instance, format_rfc3339
from core.actions import ActionResult, accumulate_action
from core.lib.state_machines import guard_require_valid_transition

supabase = get_supabase()


def _resolve_org_id(organization_name: str = None):
    """Resolve organization name to ID. No project resolution — tasks go to orgs directly."""
    if not organization_name:
        return None
    from core.services.db import get_supabase
    supabase = get_supabase()
    try:
        org_res = supabase.table('organizations').select('id').ilike('name', organization_name).limit(1).execute()
        if org_res.data:
            return org_res.data[0]['id']
    except Exception:
        pass
    return None


async def create_task_direct(
    title: str, 
    organization_id: str = None,
    organization_name: str = None,
    reminder_at: str = None, 
    priority: str = "important",
    duration_mins: int = 15,
    recurrence: str = None,
    deadline: str = None,
    direction: str = "inbound",
    committed_to: str = None,
    dedup_key: str = None,
    # Legacy params kept for backward compat — no longer used
    project_id: str = None,
    project_name: str = None,
) -> dict:
    """Direct task creation — no process_single_dump dependency.

    Tasks are assigned to orgs directly (no projects).
    Resolves organization_name to ID via DB lookup when not already provided.
    Uses entity_linker for deterministic entity resolution BEFORE creation.
    Returns {"action": "created"|"skipped"|"error", "task_id": id, "reason": str}.
    """
    task_id = None
    try:
        # Normalize dedup_key: hash to 16 chars for DB column compatibility (varchar(16))
        if dedup_key and len(dedup_key) > 16:
            dedup_key = hashlib.md5(dedup_key.encode()).hexdigest()[:16]

        if dedup_key:
            exist = supabase.table('tasks').select('id').eq('dedup_key', dedup_key) \
                .eq('is_current', True).not_.in_('status', ['done', 'cancelled']).execute()
            if exist.data:
                audit_log_sync("tools", "INFO", f"Direct create skipped (dedup): {title}")
                return {"action": "skipped", "task_id": exist.data[0]['id']}

        # Run deterministic entity resolution before resolving names
        from core.lib.entity_linker import resolve_entities
        entity_resolution = resolve_entities(
            text=title,
            planner_org_name=organization_name,
            planner_proj_name=project_name,
            write_signal_on_miss=True,
        )

        # Use resolved entities — override planner's guess with deterministic result
        if entity_resolution.organization_id:
            organization_id = entity_resolution.organization_id
            organization_name = entity_resolution.organization_name

        # Resolve org name→ID if not already provided
        if not organization_id and organization_name:
            resolved_org = _resolve_org_id(organization_name)
            if resolved_org:
                organization_id = resolved_org

        # Projects are intentionally removed. Tasks are assigned to orgs directly.
        resolved_org_id = organization_id

        insert_data = {
            "title": title,
            "status": "todo",
            "is_current": True,
            "priority": priority,
            "direction": direction,
            "duration_mins": duration_mins,
            "estimated_minutes": duration_mins,
        }
        if resolved_org_id:
            insert_data["organization_id"] = resolved_org_id
        if reminder_at:
            insert_data["reminder_at"] = reminder_at
        if deadline:
            insert_data["deadline"] = deadline
        if recurrence and recurrence not in ('none', ''):
            insert_data["recurrence"] = recurrence
        if committed_to:
            insert_data["committed_to"] = committed_to
        if dedup_key:
            insert_data["dedup_key"] = dedup_key

        res = supabase.table('tasks').insert(insert_data).execute()
        if not res.data:
            return {"action": "error", "reason": "DB insert returned no data"}

        task_id = res.data[0]['id']

        # Calendar sync if reminder_at is set
        if reminder_at:
            try:
                from core.services.google_service import check_conflict
                formatted = format_rfc3339(reminder_at)

                try:
                    conflict_title = check_conflict(reminder_at)
                    if conflict_title:
                        audit_log_sync("tools", "INFO", f"Calendar conflict detected for '{title}': overlaps with '{conflict_title}'")
                except Exception:
                    pass

                e_id = sync_to_calendar(title, formatted, duration_mins=duration_mins, priority=priority, recurrence=recurrence)
                if e_id:
                    supabase.table('tasks').update({'google_event_id': e_id}).eq('id', task_id).execute()
            except Exception as cal_e:
                audit_log_sync("tools", "WARNING", f"Calendar sync failed for task {task_id}: {cal_e}")

        # Google Tasks sync
        try:
            sync_to_google(get_tasks_service(), title=title, task_id=None, status="needsAction", due_at=deadline or reminder_at)
        except Exception as gt_e:
            audit_log_sync("tools", "WARNING", f"Google Tasks sync failed: {gt_e}")

        # Enrichment: queue graph edges + entity extraction
        from core.lib.enrichment_queue import enqueue_enrichment
        enqueue_enrichment(
            job_type="task_graph",
            target_type="task",
            target_id=task_id,
            content=title,
            related_id=None,  # Projects retired — tasks live under orgs
            related_org_id=resolved_org_id,
        )

        accumulate_action(ActionResult(action_type="task_create", status="executed", entity_id=task_id, human_label=title))
        return {"action": "created", "task_id": task_id}
    except Exception as e:
        audit_log_sync("tools", "ERROR", f"create_task_direct failed: {e}")
        try:
            from core.lib.audit_logger import write_dlq
            write_dlq("tasks", str(task_id) if task_id else "unknown", title, str(e))
        except Exception:
            pass
        return {"action": "error", "reason": str(e)}


async def create_note_direct(
    content: str,
    source: str = "executor",
    organization_id: str = None,
    organization_name: str = None,
    session_id: str = None,
    active_anchor: dict = None,
    # Legacy params kept for backward compat — no longer used
    project_id: str = None,
    project_name: str = None,
) -> dict:
    """Direct note creation — no process_single_dump dependency.

    Notes are assigned to orgs directly (no projects).
    Uses entity_linker for deterministic entity resolution.
    Falls back to planner's name→ID resolution if entity_linker misses.
    Stores thread provenance (session_id, active_anchor) in metadata for
    retroactive linking.
    Returns {"action": "filed"|"error", "memory_id": id, "reason": str}.
    """
    memory_id = None
    try:
        # ── Layer 1: Deterministic entity resolution ──
        from core.lib.entity_linker import resolve_entities
        entity_resolution = resolve_entities(
            text=content,
            planner_org_name=organization_name,
            planner_proj_name=project_name,
            write_signal_on_miss=True,
        )

        # Override planner's guess with deterministic result
        if entity_resolution.organization_id:
            organization_id = entity_resolution.organization_id
            organization_name = entity_resolution.organization_name

        audit_log_sync("tools", "INFO",
            f"create_note_direct entity resolution: org={organization_name or '(none)'} "
            f"source={entity_resolution.source}")

        # ── Layer 2: Fallback name→ID resolution ──
        if not organization_id and organization_name:
            resolved_org = _resolve_org_id(organization_name)
            if resolved_org:
                organization_id = resolved_org

        # ── Build insert data with entity context ──
        # Note: project_id is intentionally not set — notes live under orgs directly
        insert_data = {
            "content": content,
            "memory_type": "note",
            "source": source,
            "is_current": True,
            "version": 1,
        }
        if organization_id:
            insert_data["organization_id"] = organization_id

        # Build metadata with all available entity context
        metadata = {}
        if organization_id:
            metadata["organization_id"] = organization_id
        if organization_name:
            metadata["organization_name"] = organization_name
        # Store person entities from resolution (no column on memories, so metadata is the path)
        if entity_resolution.person_ids:
            metadata["person_ids"] = entity_resolution.person_ids
        if entity_resolution.person_names:
            metadata["person_names"] = entity_resolution.person_names

        # ── Layer 3: Thread provenance for retroactive linking ──
        if session_id:
            metadata["thread_id"] = session_id
        if active_anchor:
            anchor_name = active_anchor.get("name", "")
            anchor_type = active_anchor.get("type", "")
            if anchor_name:
                metadata["thread_entity_name"] = anchor_name
                metadata["thread_entity_type"] = anchor_type
                # If entity resolution didn't find an org but the thread anchor has one,
                # use the thread anchor as a third-layer fallback
                if not organization_id:
                    last_org_id = active_anchor.get("last_org_id") or active_anchor.get("organization_id")
                    if last_org_id:
                        organization_id = last_org_id
                        metadata["organization_id"] = last_org_id
                        metadata["organization_name"] = anchor_name

        if metadata:
            insert_data["metadata"] = metadata

        res = supabase.table('memories').insert(insert_data).execute()
        if not res.data:
            return {"action": "error", "reason": "DB insert returned no data"}

        memory_id = res.data[0]['id']

        # Compute expiry if content contains time-sensitive phrases
        try:
            from core.lib.time_utils import compute_expires_at
            from datetime import datetime, timezone
            expires_at = compute_expires_at(content, datetime.now(timezone.utc).isoformat())
            if expires_at:
                supabase.table('memories').update({'expires_at': expires_at}).eq('id', memory_id).execute()
        except Exception:
            pass

        # Schedule retrieval index via queue
        try:
            from core.retrieval.pipeline import schedule_index_memory
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(asyncio.to_thread(schedule_index_memory, memory_id, content, "note", source))
            else:
                schedule_index_memory(memory_id, content, "note", source)
        except Exception:
            pass

        # Enrichment: queue entity extraction + embedding
        from core.lib.enrichment_queue import enqueue_enrichment
        enqueue_enrichment(
            job_type="note_enrich",
            target_type="note",
            target_id=memory_id,
            content=content,
            related_id=source,
            related_org_id=organization_id,
        )

        accumulate_action(ActionResult(action_type="note_create", status="executed", entity_id=memory_id, human_label=content[:80]))
        return {"action": "filed", "memory_id": memory_id}
    except Exception as e:
        audit_log_sync("tools", "ERROR", f"create_note_direct failed: {e}")
        try:
            from core.lib.audit_logger import write_dlq
            write_dlq("memories", str(memory_id) if memory_id else "unknown", content, str(e))
        except Exception:
            pass
        return {"action": "error", "reason": str(e)}



def update_task_status(task_id: int, status: str = "done", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Updates a task's status (done/cancelled/todo) and reschedules it if a new reminder_at is provided."""
    try:
        task_ref = maybe_single_safe(supabase.table('tasks').select('*').eq('id', task_id))
        if not task_ref.data:
            return f"FAIL: Task {task_id} not found."
            
        td = task_ref.data
        if td.get('status') == status:
            return f"INFO: Task {task_id} already {td.get('status')}."
        if td.get('status') == 'cancelled':
            return f"FAIL: Task {task_id} was cancelled — cannot change status."

        # State machine guard
        if not guard_require_valid_transition("tasks", td.get('status', ''), status, record_id=task_id, context="update_task_status"):
            return f"FAIL: Invalid transition '{td.get('status')}' → '{status}' for task {task_id}."

        # --- RECURRING TASK: done = skip instance, cancelled = end series ---
        if td.get('recurrence') and td.get('recurrence') not in ['none', ''] and status == 'done':
            skip_msg = ""
            if td.get('google_event_id'):
                skip_msg = skip_recurring_instance(task_id)
            else:
                skip_msg = "No linked calendar event — recorded as completed."
            
            if "No upcoming instances found" not in skip_msg:
                try:
                    result = supabase.table('memories').insert({
                        'content': f"Completed instance of recurring task: {td['title']} (Task {task_id})",
                        'memory_type': 'outcome',
                        'source': 'pulse_tools'
                    }).execute()
                    memory_id = result.data[0]['id']
                    schedule_index_memory(memory_id, f"Completed instance of recurring task: {td['title']} (Task {task_id})", "outcome", "pulse_tools")
                except Exception:
                    pass
                return f"OK: Marked this week's instance done for '{td['title']}'. {skip_msg} The series continues — use 'cancelled' to end it entirely."
            
        new_reminder = format_rfc3339(reminder_at) if reminder_at else None
        g_id = td.get('google_task_id')
        e_id = td.get('google_event_id')
        
        if status in ['done', 'cancelled'] and e_id:
            delete_calendar_event(e_id)
            e_id = None
        elif new_reminder:
            e_id = sync_to_calendar(td['title'], new_reminder, event_id=e_id, duration_mins=duration_mins, priority=td.get('priority', 'important'), recurrence=recurrence or td.get('recurrence'))
        elif e_id:
            delete_calendar_event(e_id)
            e_id = None

        if g_id:
            try:
                sync_to_google(get_tasks_service(), title=td['title'], task_id=g_id, status=status, due_at=new_reminder)
            except Exception as e:
                audit_log_sync("tools", "ERROR", f"Google Tasks sync failed: {e}")

        update_payload = {"status": status, "google_event_id": e_id}
        if status == 'done':
            update_payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        
        if status == 'cancelled':
            update_payload["recurrence"] = None
        elif recurrence is not None:
            update_payload["recurrence"] = recurrence
            
        if new_reminder:
            update_payload["reminder_at"] = new_reminder

        supabase.table('tasks').update(update_payload).eq('id', task_id).execute()

        if status in ['done', 'cancelled']:
            try:
                from core.pulse.context import context_provider
                context_provider.caches['tasks'].invalidate()
                context_provider.caches['recent_tasks'].invalidate()
            except Exception:
                pass

        return f"OK: Task {task_id} updated successfully."
    except Exception as e:
        return f"FAIL: Error updating task {task_id}: {e}"

async def create_person(name: str, context: str = "", source: str = "tools") -> str:
    """Create a person row + graph node + Danny KNOWS edge."""
    try:
        existing = maybe_single_safe(
            supabase.table('people').select('id, name').ilike('name', name).eq('is_current', True)
        )
        if existing and existing.data:
            return f"Person already exists: {existing.data['name']} (ID {existing.data['id']})"

        from core.lib.people_utils import normalize_person_name
        norm_name = normalize_person_name(name)
        existing_norm = supabase.table('people').select('id, name').eq('is_current', True).execute()
        for p in (existing_norm.data or []):
            if normalize_person_name(p['name']) == norm_name:
                return f"Person already exists (normalized): {p['name']} (ID {p['id']})"

        result = supabase.table('people').insert({
            "name": name,
            "source": context or source,
            "strategic_weight": 5,
        }).execute()
        if not result or not result.data:
            return f"Failed to create person '{name}': DB insert returned no data"

        people_id = result.data[0]['id']

        from core.pulse.graph import create_graph_node_with_db_record
        gn_result = await create_graph_node_with_db_record(
            label=name,
            node_type='person',
            source_text=f"{source}:{people_id}",
            context=context,
            source_tag=source,
        )

        if gn_result.get('success'):
            audit_log_sync("tools", "INFO",
                f"Gap 5: Created person '{name}' (people_id={people_id}) with graph node")
            return f"Created person: {name} (ID {people_id})"
        else:
            audit_log_sync("tools", "WARNING",
                f"Gap 5: Person '{name}' created (ID {people_id}) but graph node failed: {gn_result.get('message')}")
            return f"Created person (partial): {name} (ID {people_id})"

    except Exception as e:
        audit_log_sync("tools", "ERROR", f"create_person failed for '{name}': {e}")
        return f"Failed to create person '{name}': {e}"


def skip_recurring_instance(task_id: int, date_str: str = None):
    """Skip (delete) a single occurrence of a recurring event/task."""
    try:
        task_ref = maybe_single_safe(supabase.table('tasks').select('id, title, recurrence, google_event_id, metadata').eq('id', task_id))
        if not task_ref.data:
            return f"Task {task_id} not found."

        td = task_ref.data
        if not td.get('recurrence'):
            return f"Task {task_id} is not a recurring event."

        e_id = td.get('google_event_id')
        if not e_id:
            return f"Task {task_id} has no linked Google Calendar event."

        from googleapiclient.discovery import build
        from core.services.google_service import get_google_creds, _MemoryCache

        service = build('calendar', 'v3', credentials=get_google_creds(), cache=_MemoryCache())

        if date_str:
            from datetime import datetime, timezone, timedelta
            target = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            time_min = target.isoformat()
            time_max = (target + timedelta(days=1)).isoformat()
        else:
            from datetime import datetime, timezone
            time_min = datetime.now(timezone.utc).isoformat()
            time_max = None

        params = {
            'calendarId': 'primary',
            'eventId': e_id,
            'timeMin': time_min,
            'singleEvents': True,
            'orderBy': 'startTime',
            'maxResults': 1,
        }
        if time_max:
            params['timeMax'] = time_max

        instances = service.events().instances(**params).execute()
        items = instances.get('items', [])
        if not items:
            return f"No upcoming instances found for recurring event '{td['title']}'."

        instance = items[0]
        instance_id = instance.get('id')
        instance_start = instance.get('start', {}).get('dateTime', 'unknown')

        try:
            existing_meta = td.get('metadata') or {}
            if isinstance(existing_meta, str):
                existing_meta = json.loads(existing_meta)
            skipped = existing_meta.get('skipped_instances', [])
            instance_date = instance_start[:10] if instance_start else ''
            if instance_date and instance_date not in skipped:
                skipped.append(instance_date)
                existing_meta['skipped_instances'] = skipped
                supabase.table('tasks').update({
                    'metadata': existing_meta
                }).eq('id', task_id).execute()
        except Exception:
            pass

        delete_calendar_instance(e_id, instance_id)
        return f"Skipped instance on {instance_start} of '{td['title']}'."
    except Exception as e:
        return f"Error skipping recurring instance: {e}"
