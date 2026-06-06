import asyncio
from datetime import datetime, timezone
from typing import List

from core.services.db import get_supabase, versioned_update
from core.lib.audit_logger import audit_log_sync
from core.services.google_service import sync_to_calendar, sync_to_google, get_tasks_service, delete_calendar_event, format_rfc3339

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
            "reminder_at": new_reminder, "dedup_key": dedup_key
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
            try:
                g_id = sync_to_google(get_tasks_service(), title, new_reminder)
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
            
        new_reminder = format_rfc3339(reminder_at) if reminder_at else None
        g_id = td.get('google_task_id')
        e_id = td.get('google_event_id')
        
        if status in ['done', 'cancelled'] and e_id:
            delete_calendar_event(e_id)
            e_id = None
        elif new_reminder:
            e_id = sync_to_calendar(td['title'], new_reminder, event_id=e_id, duration_mins=duration_mins, priority=td.get('priority', 'important'), recurrence=recurrence)
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
        res = supabase.table('people').insert({"name": name, "context": context, "status": "active"}).execute()
        if res.data:
            supabase.table('graph_nodes').insert({
                "label": name, "type": "person",
                "metadata": {"source": "pulse_tools", "person_id": str(res.data[0]['id'])}
            }).execute()
            return f"Person created with ID {res.data[0]['id']}"
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

