import os
from supabase import create_client, Client

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

def suggest_project_for_task(project_name: str) -> dict:
    """
    Given a project name, look up the project in the database.
    Returns a dict with suggested_project_id and project_confidence.
    - confidence 1.0 for exact match (case-insensitive)
    - confidence 0.7 for partial ilike match
    - None if no match found
    Fails open: if lookup fails, return None values.
    """
    if not project_name:
        return {"suggested_project_id": None, "project_confidence": None}
    
    try:
        # Check exact match first (case-insensitive, no wildcards)
        exact_res = supabase.table('projects')\
            .select('id, name')\
            .ilike('name', project_name)\
            .limit(1)\
            .execute()
        if exact_res.data:
            return {"suggested_project_id": exact_res.data[0]['id'], "project_confidence": 1.0}
        
        # Fall back to partial match
        partial_res = supabase.table('projects')\
            .select('id, name')\
            .ilike('name', f'%{project_name}%')\
            .limit(1)\
            .execute()
        if partial_res.data:
            return {"suggested_project_id": partial_res.data[0]['id'], "project_confidence": 0.7}
        
        return {"suggested_project_id": None, "project_confidence": None}
    except Exception as e:
        print(f"⚠️ Project lookup failed (failing open): {e}")
        return {"suggested_project_id": None, "project_confidence": None}
