from core.services.db import get_supabase
"""
Temporal Lineage - Version history for memories, tasks, projects.
Enables tracking how thoughts/decisions evolve over time.
"""

supabase = get_supabase()



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


