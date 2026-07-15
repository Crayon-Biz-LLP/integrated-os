import asyncio
import json
from datetime import datetime, timezone
from typing import List

from core.retrieval.pipeline import schedule_index_memory
from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import sync_to_calendar, sync_to_google, get_tasks_service, delete_calendar_event, delete_calendar_instance, format_rfc3339
from core.actions import ActionResult, accumulate_action
from core.lib.graph_rules import normalize_label
from core.lib.state_machines import guard_require_valid_transition

supabase = get_supabase()

class ToolRegistry:
    def __init__(self):
        self.tools = {}
        
    def register(self, func):
        self.tools[func.__name__] = func
        return func
        
    def get_tools_list(self):
        return list(self.tools.values())
        
    async def execute_tool_call(self, function_call):
        name = function_call.name
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
            
        args = function_call.args
        if hasattr(args, "model_dump"):
            args = args.model_dump()
            
        func = self.tools[name]
        if asyncio.iscoroutinefunction(func):
            return await func(**args)
        else:
            return func(**args)

rhodey_tools = ToolRegistry()

class HitlInterrupt(Exception):
    def __init__(self, action: str, reason: str, pending_id: str = None):
        self.action = action
        self.reason = reason
        self.pending_id = pending_id

# -----------------
# Rhodey Tools
# -----------------

@rhodey_tools.register
def ask_user_approval(action: str, reason: str):
    """Ask the user for approval before performing a sensitive action. The run pauses here."""
    raise HitlInterrupt(action, reason)

@rhodey_tools.register
def save_briefing(text: str):
    """Saves the final strategic briefing text to the system for the user to read."""
    supabase.table('core_config').upsert({
        "key": "latest_briefing",
        "content": text
    }, on_conflict="key").execute()
    return "Briefing saved successfully."

@rhodey_tools.register
def create_project(name: str, description: str = "", keywords: List[str] = None, organization_name: str = None, client_organization_name: str = None):
    """Creates a new project. Optionally provide organization_name and client_organization_name for proper org routing."""
    from core.features import is_org_routing_enabled
    
    try:
        data = {
            "name": name,
            "description": description,
            "context": "work",
            "status": "active",
            "is_active": True,
            "keywords": keywords or []
        }
        
        org_id = None
        client_org_id = None
        
        if is_org_routing_enabled() and (organization_name or client_organization_name):
            orgs_res = supabase.table('organizations').select('id, name').execute()
            orgs_dict = {o['name'].lower(): o['id'] for o in (orgs_res.data or [])}

            if organization_name:
                if organization_name.lower() in orgs_dict:
                    org_id = orgs_dict[organization_name.lower()]
                    data['organization_id'] = org_id
                else:
                    # Unknown org — write signal so Pulse can surface it, then reject
                    try:
                        supabase.table('project_creation_signals').insert({
                            "project_name": f"{name} [unknown_org={organization_name}]",
                            "source": "create_project_tool",
                        }).execute()
                    except Exception as sig_e:
                        audit_log_sync("tools", "WARNING", f"Failed to write project_creation_signal: {sig_e}")
                    return (
                        f"Error creating project: organization '{organization_name}' not found. "
                        "Ask the user to approve this org via Decisions first, or use an existing org name."
                    )

            if client_organization_name:
                if client_organization_name.lower() in orgs_dict:
                    client_org_id = orgs_dict[client_organization_name.lower()]
                else:
                    return (
                        f"Error creating project: client organization '{client_organization_name}' not found. "
                        "Ask the user to approve this org via Decisions first, or use an existing org name."
                    )

        res = supabase.table('projects').insert(data).execute()
        
        if res.data:
            proj_id = res.data[0]['id']
            
            if is_org_routing_enabled():
                if org_id:
                    supabase.table('project_organizations').insert({
                        "project_id": proj_id,
                        "organization_id": org_id,
                        "role": "performer"
                    }).execute()
                if client_org_id and client_org_id != org_id:
                    supabase.table('project_organizations').insert({
                        "project_id": proj_id,
                        "organization_id": client_org_id,
                        "role": "client"
                    }).execute()
            
            # Create graph node
            supabase.table('graph_nodes').insert({
                "label": name,
                "type": "project",
                "normalized_label": normalize_label(name),
                "metadata": {"source": "pulse_tools", "project_id": str(proj_id)}
            }).execute()
            return f"Project created with ID {proj_id}"
    except Exception as e:
        return f"Error creating project: {str(e)}"
    return "Failed to create project."

@rhodey_tools.register

def _resolve_project_and_org_id(project_name: str = None, organization_name: str = None):
    """Resolve names to (project_id, organization_id)."""
    if not project_name and not organization_name:
        return None, None
        
    from core.services.db import fetch_active_projects, get_supabase
    supabase = get_supabase()
    
    project_id = None
    org_id = None
    
    if project_name:
        projects = fetch_active_projects()
        for p in projects:
            if p['name'].lower() == project_name.lower():
                project_id = p['id']
                break
                
    if organization_name and not org_id:
        try:
            org_res = supabase.table('organizations').select('id').ilike('name', organization_name).limit(1).execute()
            if org_res.data:
                org_id = org_res.data[0]['id']
        except Exception:
            pass
            
    # Also log to project_creation_signals if organization missing
    from core.features import is_org_routing_enabled
    if is_org_routing_enabled() and project_name and not project_id and not org_id:
        try:
            supabase.table('project_creation_signals').insert({
                "project_name": f"{project_name} [unknown_org={organization_name}]" if organization_name else project_name,
                "source": "tools_resolver"
            }).execute()
        except Exception:
            pass
            
    return project_id, org_id

async def create_task_direct(
    title: str, 
    project_id: str = None, 
    organization_id: str = None,
    project_name: str = None,
    organization_name: str = None,
    reminder_at: str = None, 
    priority: str = "important",
    duration_mins: int = 15,
    recurrence: str = None,
    deadline: str = None,
    direction: str = "inbound",
    committed_to: str = None,
    dedup_key: str = None,
) -> dict:
    """Direct task creation — no process_single_dump dependency.

    Inserts directly into tasks table with minimal dedup check.
    Resolves project_name/organization_name to IDs via DB lookup
    when project_id/organization_id are not already provided.
    Returns {"action": "created"|"skipped"|"error", "task_id": id, "reason": str}.
    """
    try:
        if dedup_key:
            exist = supabase.table('tasks').select('id').eq('dedup_key', dedup_key) \
                .eq('is_current', True).not_.in_('status', ['done', 'cancelled']).execute()
            if exist.data:
                audit_log_sync("tools", "INFO", f"Direct create skipped (dedup): {title}")
                return {"action": "skipped", "task_id": exist.data[0]['id']}

        # Resolve name→ID if IDs not provided but names are
        if (not project_id or not organization_id) and (project_name or organization_name):
            
            resolved_proj, resolved_org = _resolve_project_and_org_id(project_name, organization_name)
            if not project_id:
                project_id = resolved_proj
            if not organization_id:
                organization_id = resolved_org

        resolved_project_id = project_id
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
        if resolved_project_id:
            insert_data["project_id"] = resolved_project_id
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
                from core.services.google_service import sync_to_calendar, format_rfc3339
                formatted = format_rfc3339(reminder_at)
                e_id = sync_to_calendar(title, formatted, duration_mins=duration_mins, priority=priority, recurrence=recurrence)
                if e_id:
                    supabase.table('tasks').update({'google_event_id': e_id}).eq('id', task_id).execute()
            except Exception as cal_e:
                audit_log_sync("tools", "WARNING", f"Calendar sync failed for task {task_id}: {cal_e}")

        # Google Tasks sync
        try:
            from core.services.google_service import sync_to_google, get_tasks_service
            sync_to_google(get_tasks_service(), title=title, task_id=None, status="needsAction", due_at=deadline or reminder_at)
        except Exception as gt_e:
            audit_log_sync("tools", "WARNING", f"Google Tasks sync failed: {gt_e}")

        # Enrichment: graph edges + entity extraction (fire-and-forget, non-blocking)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_enrich_task_for_graph(
                    task_id=task_id, title=title, text=title, project_id=resolved_project_id
                ))
        except Exception:
            pass

        accumulate_action(ActionResult(action_type="task_create", status="executed", entity_id=task_id, human_label=title))
        return {"action": "created", "task_id": task_id}
    except Exception as e:
        audit_log_sync("tools", "ERROR", f"create_task_direct failed: {e}")
        return {"action": "error", "reason": str(e)}


async def _enrich_task_for_graph(task_id: int, title: str, text: str, project_id: str = None):
    """Background enrichment: graph edges + entity extraction for newly created tasks."""
    try:
        from core.pulse.graph import write_graph_edges_for_task
        from core.pulse.entity_extractor import extract_and_link_entities
        await write_graph_edges_for_task(task_id, title, project_id or "")
        await extract_and_link_entities(text, task_id, 'task')
        audit_log_sync("tools", "INFO", f"Enriched task {task_id}: graph edges + entities")
    except Exception as e:
        audit_log_sync("tools", "WARNING", f"Task enrichment failed for {task_id}: {e}")


async def _enrich_note_for_graph(memory_id: int, content: str, source: str):
    """Background enrichment: entity extraction + embedding for newly created notes."""
    try:
        from core.pulse.entity_extractor import extract_and_link_entities
        from core.llm import get_embedding
        
        # Entity extraction
        await extract_and_link_entities(content, memory_id, 'memory')
        
        # Embedding generation (inline — important for immediate searchability)
        try:
            embedding_res = await get_embedding(content)
            embedding = embedding_res.vector if embedding_res else None
            if embedding:
                supabase.table('memories').update({
                    "embedding": embedding
                }).eq('id', memory_id).eq('is_current', True).execute()
        except Exception as emb_e:
            audit_log_sync("tools", "WARNING", f"Embedding gen failed for note {memory_id}: {emb_e}")
        
        audit_log_sync("tools", "INFO", f"Enriched note {memory_id}: entities + embedding")
    except Exception as e:
        audit_log_sync("tools", "WARNING", f"Note enrichment failed for {memory_id}: {e}")


@rhodey_tools.register
async def create_note_direct(content: str, source: str = "executor", project_id: str = None, organization_id: str = None, project_name: str = None, organization_name: str = None) -> dict:
    """Direct note creation — no process_single_dump dependency.

    Inserts directly into memories table.
    Resolves project_name/organization_name to IDs via DB lookup
    when project_id/organization_id are not already provided.
    Returns {"action": "filed"|"error", "memory_id": id, "reason": str}.
    """
    try:
        # Resolve name→ID if IDs not provided but names are
        if (not project_id or not organization_id) and (project_name or organization_name):
            
            resolved_proj, resolved_org = _resolve_project_and_org_id(project_name, organization_name)
            if not project_id:
                project_id = resolved_proj
            if not organization_id:
                organization_id = resolved_org

        insert_data = {
            "content": content,
            "memory_type": "note",
            "source": source,
            "is_current": True,
            "version": 1,
        }
        if project_id:
            insert_data["metadata"] = {"project_id": project_id}
        if organization_id:
            if "metadata" not in insert_data:
                insert_data["metadata"] = {}
            insert_data["metadata"]["organization_id"] = organization_id

        res = supabase.table('memories').insert(insert_data).execute()
        if not res.data:
            return {"action": "error", "reason": "DB insert returned no data"}

        memory_id = res.data[0]['id']

        # Schedule retrieval index as background task
        try:
            import asyncio
            from core.retrieval.pipeline import schedule_index_memory
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(asyncio.to_thread(schedule_index_memory, memory_id, content, "note", source))
        except Exception:
            pass

        # Enrichment: entity extraction + embedding (fire-and-forget, non-blocking)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_enrich_note_for_graph(
                    memory_id=memory_id, content=content, source=source
                ))
        except Exception:
            pass

        accumulate_action(ActionResult(action_type="note_create", status="executed", entity_id=memory_id, human_label=content[:80]))
        return {"action": "filed", "memory_id": memory_id}
    except Exception as e:
        audit_log_sync("tools", "ERROR", f"create_note_direct failed: {e}")
        return {"action": "error", "reason": str(e)}


@rhodey_tools.register
async def create_task(title: str, project_id: int = None, organization_name: str = None, priority: str = "important", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Creates a new task and optionally schedules it on the calendar.

    Delegates to create_task_direct which handles name→ID resolution,
    calendar sync, Google Tasks sync, and enrichment (graph edges +
    entity extraction) via the single, unified task creation path.
    """
    from core.features import is_org_routing_enabled

    project_name = None
    if project_id:
        p_res = supabase.table('projects').select('name').eq('id', project_id).limit(1).execute()
        if p_res.data:
            project_name = p_res.data[0]['name']

    org_unresolved = False
    if is_org_routing_enabled() and organization_name and not project_name:
        project_name = organization_name

    result = await create_task_direct(
        title=title,
        project_name=project_name,
        organization_name=organization_name,
        reminder_at=reminder_at,
        priority=priority,
        duration_mins=duration_mins,
        recurrence=recurrence,
    )

    if result.get("action") == "error":
        accumulate_action(ActionResult(action_type="task_create", status="failed", evidence={"error": result.get('reason')}))
        return f"Error creating task: {result.get('reason')}"

    task_id = result.get("task_id")
    if not task_id:
        return "Failed to create task."

    accumulate_action(ActionResult(action_type="task_create", status="executed", entity_id=task_id, human_label=title))
    return f"Task created with ID {task_id}" + (
        f" (WARNING: organization '{organization_name}' not found — task has no org routing. "
        "Approve this org via Decisions first.)" if org_unresolved else ""
    )

@rhodey_tools.register
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
            # Skip the next instance and record an outcome, but keep the series alive
            skip_msg = ""
            if td.get('google_event_id'):
                skip_msg = skip_recurring_instance(task_id)
            else:
                skip_msg = "No linked calendar event — recorded as completed."
            
            # If the series is exhausted (UNTIL date passed), we fall through to complete the master task.
            if "No upcoming instances found" not in skip_msg:
                # Write an outcome memory
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
        
        # When cancelled, always clear the recurrence so the RRULE doesn't linger
        if status == 'cancelled':
            update_payload["recurrence"] = None
        elif recurrence is not None:
            update_payload["recurrence"] = recurrence
            
        if new_reminder:
            update_payload["reminder_at"] = new_reminder

        supabase.table('tasks').update(update_payload).eq('id', task_id).execute()
        return f"OK: Task {task_id} updated successfully."
    except Exception as e:
        return f"FAIL: Error updating task {task_id}: {e}"

@rhodey_tools.register
def create_person(name: str, context: str):
    """Records a new person in the Knowledge Graph."""
    try:
        existing_node = maybe_single_safe(supabase.table('pending_nodes').select('id').eq('label', name).eq('node_type', 'person').in_('status', ['pending', 'flagged']))
        if existing_node and existing_node.data:
            return f"Person '{name}' already pending approval (ID: {existing_node.data['id']})."

        existing_live = maybe_single_safe(supabase.table('graph_nodes').select('id, db_record_id, canonical_id').eq('label', name).eq('type', 'person').eq('is_current', True))
        if existing_live and existing_live.data:
            if existing_live.data.get('canonical_id'):
                from core.lib.graph_rules import get_canonical_id
                c_id = get_canonical_id(existing_live.data['id'])
                c_node = maybe_single_safe(supabase.table('graph_nodes').select('db_record_id').eq('id', c_id))
                if c_node and c_node.data and c_node.data.get('db_record_id'):
                    return f"Person '{name}' was merged. ID {c_node.data['db_record_id']}"
            db_id = existing_live.data.get('db_record_id')
            return f"Person '{name}' already exists in graph.{' ID ' + str(db_id) if db_id else ''}"

        res = supabase.table('pending_nodes').insert({
            "label": name,
            "type": "person",
            "status": "pending",
            "source_text": f"pulse_tools: {context[:100]}",
            "metadata": {"source": "pulse_tools", "context": context}
        }).execute()
        if res.data:
            pending_id = res.data[0]['id']
            from core.lib.graph_rules import insert_pending_edge
            insert_pending_edge(
                "Danny",
                name,
                "KNOWS",
                {"source_text": "pulse_tools_create_person", "source_type": "person", "target_type": "person"}
            )
            return f"Person '{name}' queued for approval (pending ID: {pending_id})."
    except Exception as e:
        return f"Error: {e}"
    return "Failed to create person."

@rhodey_tools.register
def link_resource_to_cluster(resource_id: int, cluster_name: str):
    """Links an existing resource to a cluster (creating the cluster if it doesn't exist)."""
    try:
        exist = maybe_single_safe(supabase.table('clusters').select('id').ilike('title', cluster_name))
        if exist.data:
            c_id = exist.data['id']
        else:
            c_res = supabase.table('clusters').insert({"title": cluster_name, "status": "active"}).execute()
            c_id = c_res.data[0]['id']
            
        supabase.table('resources').update({"cluster_id": c_id}).eq('id', resource_id).execute()
        return f"Resource {resource_id} linked to cluster {cluster_name}"
    except Exception as e:
        return f"Error linking resource: {e}"

@rhodey_tools.register
def log_audit_message(message: str, level: str = "INFO"):
    """Logs an internal system message for observability."""
    audit_log_sync("pulse_agent", level, message)
    return "Logged."


@rhodey_tools.register
def skip_recurring_instance(task_id: int, date_str: str = None):
    """Skip (delete) a single occurrence of a recurring event/task.
    If no date_str is provided, the next upcoming instance is skipped.
    date_str format: YYYY-MM-DD (optional, defaults to next instance)."""
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

        # T4: Store skipped instance date to prevent ghost re-creation
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

