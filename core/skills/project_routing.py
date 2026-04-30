import os
from supabase import create_client, Client

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

def suggest_project_for_task(project_name: str) -> dict:
    """
    Given a project name, look up the project in the database.
    Returns a dict with suggested_project_id (or None if not found).
    Fails open: if lookup fails, return None for ID.
    """
    if not project_name:
        return {"suggested_project_id": None}
    
    try:
        project_res = supabase.table('projects')\
            .select('id, name')\
            .ilike('name', f'%{project_name}%')\
            .maybe_single()\
            .execute()
        if getattr(project_res, 'data', None):
            return {"suggested_project_id": project_res.data['id']}
        return {"suggested_project_id": None}
    except Exception as e:
        print(f"⚠️ Project lookup failed (failing open): {e}")
        return {"suggested_project_id": None}
