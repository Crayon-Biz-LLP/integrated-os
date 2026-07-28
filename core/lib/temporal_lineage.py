from core.services.db import get_supabase
"""
Temporal Lineage - Version history for memories, tasks, projects.
Enables tracking how thoughts/decisions evolve over time.
"""

supabase = get_supabase()



def detect_drift(entity_name: str, hours_window: int = 48) -> dict:
    """
    Detect if a project/organization has been updated too frequently.
    
    Args:
        entity_name: Name of the project or organization to check
        hours_window: Lookback window in hours (default 48)
    
    Returns:
        Dict with update_count, first_update, last_update
    """
    result = supabase.rpc("detect_drift", {
        "project_name": entity_name,
        "hours_window": hours_window
    }).execute()
    
    if result.data:
        return {
            "update_count": result.data[0].get("update_count", 0),
            "first_update": result.data[0].get("first_update"),
            "last_update": result.data[0].get("last_update")
        }
    return {"update_count": 0, "first_update": None, "last_update": None}


