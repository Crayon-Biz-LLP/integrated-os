"""
Temporal Lineage - Version history for memories, tasks, projects.
Enables tracking how thoughts/decisions evolve over time.
"""
import os
from datetime import datetime, timezone
from supabase import create_client, Client

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)


def create_versioned_memory(
    content: str,
    memory_type: str,
    project_id: int = None,
    old_memory_id: int = None,
    metadata: dict = None,
    embedding: list = None
) -> dict:
    """
    Create a new memory version instead of updating.
    
    Args:
        content: New memory content
        memory_type: Type of memory
        project_id: Associated project
        old_memory_id: ID of memory being superseded (None for new memories)
        metadata: Additional metadata
        embedding: Pre-computed embedding vector
        
    Returns:
        New memory record
    """
    # Get next version number if updating existing
    version = 1
    if old_memory_id:
        old = supabase.table("memories").select("version").eq("id", old_memory_id).execute()
        if old.data:
            version = (old.data[0].get("version", 0) or 0) + 1
    
    # Mark old memory as not current
    if old_memory_id:
        supabase.table("memories").update({
            "is_current": False
        }).eq("id", old_memory_id).execute()
    
    # Create new version
    new_memory = {
        "content": content,
        "memory_type": memory_type,
        "project_id": project_id,
        "version": version,
        "is_current": True,
        "supersedes_id": old_memory_id,
        "metadata": metadata or {},
        "embedding": embedding,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    result = supabase.table("memories").insert(new_memory).execute()
    return result.data[0] if result.data else None


def create_versioned_task(
    title: str,
    project_id: int,
    old_task_id: int = None,
    **kwargs
) -> dict:
    """
    Create a new task version instead of updating.
    
    Args:
        title: Task title
        project_id: Project ID
        old_task_id: ID of task being superseded
        **kwargs: Other task fields (priority, status, etc.)
        
    Returns:
        New task record
    """
    # Get next version number
    version = 1
    if old_task_id:
        old = supabase.table("tasks").select("version").eq("id", old_task_id).execute()
        if old.data:
            version = (old.data[0].get("version", 0) or 0) + 1
    
    # Create new version
    new_task = {
        "title": title,
        "project_id": project_id,
        "version": version,
        "is_current": True,
        "supersedes_id": old_task_id,
        **kwargs
    }
    
    # Insert new version FIRST (so failure doesn't orphan the old record)
    result = supabase.table("tasks").insert(new_task).execute()
    
    # Mark old task as not current (only after new insert succeeds)
    if old_task_id:
        supabase.table("tasks").update({
            "is_current": False
        }).eq("id", old_task_id).execute()
    
    return result.data[0] if result.data else None


def create_versioned_project(
    name: str,
    org_tag: str,
    old_project_id: int = None,
    **kwargs
) -> dict:
    """
    Create a new project version instead of updating.
    
    Args:
        name: Project name
        org_tag: Organization tag
        old_project_id: ID of project being superseded
        **kwargs: Other project fields
        
    Returns:
        New project record
    """
    # Get next version number
    version = 1
    if old_project_id:
        old = supabase.table("projects").select("version").eq("id", old_project_id).execute()
        if old.data:
            version = (old.data[0].get("version", 0) or 0) + 1
    
    # Mark old project as not current
    if old_project_id:
        supabase.table("projects").update({
            "is_current": False
        }).eq("id", old_project_id).execute()
    
    # Create new version
    new_project = {
        "name": name,
        "org_tag": org_tag,
        "version": version,
        "is_current": True,
        "supersedes_id": old_project_id,
        **kwargs
    }
    
    result = supabase.table("projects").insert(new_project).execute()
    return result.data[0] if result.data else None


def get_memory_history(memory_id: int) -> list:
    """
    Get full version history of a memory.
    
    Returns:
        List of versions (oldest first)
    """
    history = []
    current_id = memory_id
    
    # Walk the supersedes chain backward
    while current_id:
        mem = supabase.table("memories").select("*").eq("id", current_id).execute()
        if not mem.data:
            break
        history.insert(0, mem.data[0])  # Insert at beginning (oldest first)
        current_id = mem.data[0].get("supersedes_id")
    
    return history


def detect_drift(project_name: str, hours_window: int = 48) -> dict:
    """
    Detect if a project goal has been updated too frequently.
    
    Returns:
        Dict with update_count, first_update, last_update
    """
    result = supabase.rpc("detect_drift", {
        "project_name": project_name,
        "hours_window": hours_window
    }).execute()
    
    if result.data:
        return {
            "update_count": result.data[0].get("update_count", 0),
            "first_update": result.data[0].get("first_update"),
            "last_update": result.data[0].get("last_update")
        }
    return {"update_count": 0, "first_update": None, "last_update": None}


def get_state_at_time(table_name: str, record_id: int, query_time: str) -> dict:
    """
    Get the state of a record at a specific point in time.
    
    Args:
        table_name: 'memories', 'tasks', or 'projects'
        record_id: The record ID
        query_time: ISO timestamp string
        
    Returns:
        The record state at that time
    """
    # Query for the version that was current at query_time
    result = supabase.table(table_name) \
        .select("*") \
        .eq("id", record_id) \
        .lte("created_at", query_time) \
        .order("version", desc=False) \
        .limit(1) \
        .execute()
    
    if result.data:
        return result.data[0]
    
    # If not found by ID, check supersedes chain
    result = supabase.table(table_name) \
        .select("*") \
        .eq("supersedes_id", record_id) \
        .lte("created_at", query_time) \
        .order("created_at", desc=False) \
        .limit(1) \
        .execute()
    
    return result.data[0] if result.data else None
