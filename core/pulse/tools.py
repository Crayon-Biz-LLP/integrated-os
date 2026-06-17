import asyncio
from datetime import datetime, timezone
from typing import List

from core.services.db import get_supabase, versioned_update
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import sync_to_calendar, sync_to_google, get_tasks_service, delete_calendar_event, delete_calendar_instance, format_rfc3339

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
def create_project(name: str, org_tag: str, description: str = "", keywords: List[str] = None):
    """Creates a new project. Valid org_tags: SOLVSTRAT, QHORD, PERSONAL, CRAYON, ASHRAYA."""
    valid_tags = ['SOLVSTRAT', 'QHORD', 'PERSONAL', 'CRAYON', 'ASHRAYA']
    CONTEXT_MAP = {'ASHRAYA': 'personal', 'PERSONAL': 'personal', 'SOLVSTRAT': 'work', 'QHORD': 'work', 'CRAYON': 'work'}
    
    org_tag = org_tag.upper() if org_tag.upper() in valid_tags else 'SOLVSTRAT'
    
    try:
        res = supabase.table('projects').insert({
            "name": name,
            "org_tag": org_tag,
            "description": description,
            "context": CONTEXT_MAP.get(org_tag, 'work'),
            "status": "active",
            "is_active": True,
            "keywords": keywords or []
        }).execute()
        
        if res.data:
            proj_id = res.data[0]['id']
            # Create graph node
            supabase.table('graph_nodes').insert({
                "label": name,
                "type": "project",
                "metadata": {"source": "pulse_tools", "project_id": str(proj_id), "org_tag": org_tag}
            }).execute()
            return f"Project created with ID {proj_id}"
    except Exception as e:
        return f"Error creating project: {str(e)}"
    return "Failed to create project."

@rhodey_tools.register
def create_task(title: str, project_id: int = None, priority: str = "important", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Creates a new task and optionally schedules it on the calendar."""
    import hashlib
    dedup_key = hashlib.md5(f"{title.lower().strip()}:{project_id or 0}".encode()).hexdigest()[:16]
    
    exist = supabase.table('tasks').select('id').eq('dedup_key', dedup_key).not_.in_('status', ['done', 'cancelled']).execute()
    if exist.data:
        return f"Task '{title}' already exists."
        
    try:
        new_reminder = format_rfc3339(reminder_at) if reminder_at else None
        data = {
            "title": title, "project_id": project_id, "priority": priority.lower(),
            "status": "todo", "estimated_minutes": duration_mins, "duration_mins": duration_mins,
            "reminder_at": new_reminder, "dedup_key": dedup_key,
            "recurrence": recurrence
        }
        res = supabase.table('tasks').insert(data).execute()
        if not res.data:
            return "Failed to create task."
            
        task_id = res.data[0]['id']
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
            
        return f"Task created with ID {task_id}"
    except Exception as e:
        return f"Error creating task: {str(e)}"

@rhodey_tools.register
def update_task_status(task_id: int, status: str = "done", duration_mins: int = 15, reminder_at: str = None, recurrence: str = None):
    """Updates a task's status (done/cancelled/todo) and reschedules it if a new reminder_at is provided."""
    try:
        task_ref = supabase.table('tasks').select('*').eq('id', task_id).maybe_single().execute()
        if not task_ref.data:
            return f"Task {task_id} not found."
            
        td = task_ref.data
        if td.get('status') in ['done', 'cancelled'] and status in ['done', 'cancelled']:
            return f"Task {task_id} already {td.get('status')}."

        # --- RECURRING TASK: done = skip instance, cancelled = end series ---
        if td.get('recurrence') and status == 'done':
            # Skip the next instance and record an outcome, but keep the series alive
            skip_msg = ""
            if td.get('google_event_id'):
                skip_msg = skip_recurring_instance(task_id)
            else:
                skip_msg = "No linked calendar event — recorded as completed."
            # Write an outcome memory
            try:
                supabase.table('memories').insert({
                    'content': f"Completed instance of recurring task: {td['title']} (Task {task_id})",
                    'memory_type': 'outcome',
                    'source': 'pulse_tools'
                }).execute()
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

        versioned_update('tasks', task_id, update_payload, change_source='pulse_tools')
        return f"Task {task_id} updated successfully."
    except Exception as e:
        return f"Error updating task {task_id}: {e}"

@rhodey_tools.register
def create_person(name: str, context: str):
    """Records a new person in the Knowledge Graph."""
    try:
        res = supabase.table('pending_graph_nodes').insert({
            "label": name,
            "type": "person",
            "status": "pending",
            "source_text": f"pulse_tools: {context[:100]}",
            "metadata": {"source": "pulse_tools", "context": context}
        }).execute()
        if res.data:
            pending_id = res.data[0]['id']
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
        exist = supabase.table('clusters').select('id').ilike('title', cluster_name).maybe_single().execute()
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
        task_ref = supabase.table('tasks').select('id, title, recurrence, google_event_id').eq('id', task_id).maybe_single().execute()
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

        delete_calendar_instance(e_id, instance_id)
        return f"Skipped instance on {instance_start} of '{td['title']}'."
    except Exception as e:
        return f"Error skipping recurring instance: {e}"

