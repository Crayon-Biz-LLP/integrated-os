import os
from supabase import create_client, Client

_supabase: Client = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )
    return _supabase



def fetch_active_projects() -> list:
    supabase = get_supabase()
    try:
        res = supabase.table('projects').select('id, name, org_tag').eq('status', 'active').execute()
        return res.data or []
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("db", "WARNING", f"Failed to fetch projects: {e}")
        return []


def zombie_recovery():
    from datetime import datetime, timezone, timedelta
    supabase = get_supabase()
    try:
        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        supabase.table('raw_dumps') \
            .update({"status": "staged"}) \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        # Also recover orphaned completion dumps stuck in processing_completion
        supabase.table('raw_dumps') \
            .update({"status": "awaiting_completion_match"}) \
            .eq('status', 'processing_completion') \
            .lt('created_at', ten_mins_ago) \
            .execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("db", "WARNING", f"Zombie recovery failed: {e}")


