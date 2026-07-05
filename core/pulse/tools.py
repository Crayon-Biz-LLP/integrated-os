import asyncio
import json
from datetime import datetime, timezone
from typing import List

from core.retrieval.pipeline import schedule_index_memory
from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import sync_to_calendar, sync_to_google, get_tasks_service, delete_calendar_event, delete_calendar_instance, format_rfc3339
from core.actions import ActionResult, accumulate_action

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
                "metadata": {"source": "pulse_tools", "project_id": str(proj_id)}
            }).execute()
            return f"Project created with ID {proj_id}"
    except Exception as e:
        return f"Error creating project: {str(e)}"
    return "Failed to create project."

@rhodey_tools.register
def create_task(title: str, project_id: int = None, organization_name: str = None, priority: str = "important", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Creates a new task and optionally schedules it on the calendar."""
    from core.features import is_org_routing_enabled
    import hashlib
    dedup_key = hashlib.md5(f"{title.lower().strip()}:{project_id or 0}".encode()).hexdigest()[:16]
    
    exist = supabase.table('tasks').select('id').eq('is_current', True).eq('dedup_key', dedup_key).not_.in_('status', ['done', 'cancelled']).execute()
    if exist.data:
        return f"Task '{title}' already exists."
        
    try:
        new_reminder = format_rfc3339(reminder_at) if reminder_at else None
        
        org_id = None
        org_unresolved = False
        if is_org_routing_enabled() and organization_name:
            orgs_res = supabase.table('organizations').select('id').ilike('name', organization_name).limit(1).execute()
            if orgs_res.data:
                org_id = orgs_res.data[0]['id']
            else:
                org_unresolved = True

        data = {
            "title": title, "project_id": project_id, "priority": priority.lower(),
            "status": "todo", "estimated_minutes": duration_mins, "duration_mins": duration_mins,
            "reminder_at": new_reminder, "dedup_key": dedup_key,
            "recurrence": recurrence,
            "organization_id": org_id
        }
        res = supabase.table('tasks').insert(data).execute()
        if not res.data:
            return "Failed to create task."
            
        task_id = res.data[0]['id']
        accumulate_action(ActionResult(action_type="task_create", status="executed", entity_id=task_id, human_label=title))
        e_id, g_id = None, None
        
        if new_reminder:
            try:
                e_id = sync_to_calendar(title, new_reminder, duration_mins, priority=priority, recurrence=recurrence)
            except Exception as e:
                audit_log_sync("tools", "ERROR", f"Calendar sync failed: {e}")
            # Recurring tasks with a time (calendar event) skip Google Tasks —
            # the calendar series handles scheduling. Day-only recurring tasks
            # (no reminder_at) still get a google_task_id for lightweight tracking.
            if not recurrence:
                try:
                    g_id = sync_to_google(get_tasks_service(), title, new_reminder)
                except Exception as e:
                    audit_log_sync("tools", "ERROR", f"Tasks sync failed: {e}")
        elif recurrence:
            # Day-only recurring task — no calendar event, but create a Google Task
            try:
                g_id = sync_to_google(get_tasks_service(), title)
            except Exception as e:
                audit_log_sync("tools", "ERROR", f"Tasks sync failed: {e}")
                
        if e_id or g_id:
            update = {}
            if e_id:
                update['google_event_id'] = e_id
            if g_id:
                update['google_task_id'] = g_id
            supabase.table('tasks').update(update).eq('id', task_id).execute()
            
        return f"Task created with ID {task_id}" + (
            f" (WARNING: organization '{organization_name}' not found — task has no org routing. "
            "Approve this org via Decisions first.)" if org_unresolved else ""
        )
    except Exception as e:
        accumulate_action(ActionResult(action_type="task_create", status="failed", evidence={"error": str(e)}))
        return f"Error creating task: {str(e)}"

@rhodey_tools.register
def update_task_status(task_id: int, status: str = "done", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Updates a task's status (done/cancelled/todo) and reschedules it if a new reminder_at is provided."""
    try:
        task_ref = maybe_single_safe(supabase.table('tasks').select('*').eq('id', task_id))
        if not task_ref.data:
            return f"Task {task_id} not found."
            
        td = task_ref.data
        if td.get('status') in ['done', 'cancelled'] and status in ['done', 'cancelled']:
            return f"Task {task_id} already {td.get('status')}."

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
                return f"Marked this week's instance done for '{td['title']}'. {skip_msg} The series continues — use 'cancelled' to end it entirely."
            
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
        if new_reminder:
            update_payload["reminder_at"] = new_reminder

        supabase.table('tasks').update(update_payload).eq('id', task_id).execute()
        return f"Task {task_id} updated successfully."
    except Exception as e:
        return f"Error updating task {task_id}: {e}"

@rhodey_tools.register
def create_person(name: str, context: str):
    """Records a new person in the Knowledge Graph."""
    try:
        existing_node = maybe_single_safe(supabase.table('pending_graph_nodes').select('id').eq('label', name).eq('type', 'person').in_('status', ['pending', 'flagged']))
        if existing_node and existing_node.data:
            return f"Person '{name}' already pending approval (ID: {existing_node.data['id']})."

        existing_live = maybe_single_safe(supabase.table('graph_nodes').select('id, db_record_id, canonical_id').eq('label', name).eq('type', 'person'))
        if existing_live and existing_live.data:
            if existing_live.data.get('canonical_id'):
                from core.lib.graph_rules import get_canonical_id
                c_id = get_canonical_id(existing_live.data['id'])
                c_node = maybe_single_safe(supabase.table('graph_nodes').select('db_record_id').eq('id', c_id))
                if c_node and c_node.data and c_node.data.get('db_record_id'):
                    return f"Person '{name}' was merged. ID {c_node.data['db_record_id']}"
            db_id = existing_live.data.get('db_record_id')
            return f"Person '{name}' already exists in graph.{' ID ' + str(db_id) if db_id else ''}"

        res = supabase.table('pending_graph_nodes').insert({
            "label": name,
            "type": "person",
            "status": "pending",
            "source_text": f"pulse_tools: {context[:100]}",
            "metadata": {"source": "pulse_tools", "context": context}
        }).execute()
        if res.data:
            pending_id = res.data[0]['id']
            existing_knows = maybe_single_safe(supabase.table('pending_graph_edges').select('id').eq('source_label', 'Danny').eq('target_label', name).eq('relationship', 'KNOWS').in_('status', ['pending', 'approved']))
            if not (existing_knows and existing_knows.data):
                supabase.table('pending_graph_edges').insert({
                    "source_label": "Danny",
                    "target_label": name,
                    "relationship": "KNOWS",
                    "status": "pending",
                    "source_text": "pulse_tools_create_person"
                }).execute()
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

