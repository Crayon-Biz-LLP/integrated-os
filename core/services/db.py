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
        res = supabase.table('projects').select('id, name, organization_id').eq('status', 'active').execute()
        return res.data or []
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("db", "WARNING", f"Failed to fetch projects: {e}")
        return []


def version_memory_for_update(memory_id: int, update_data: dict) -> dict:
    """
    Archive the current memory row (is_current=false) and return update_data
    augmented with bumped version and supersedes_id.
    Caller must apply the returned dict via .update() on the live row.
    If the memory doesn't exist or is already archived, returns update_data as-is.
    """
    supabase = get_supabase()
    try:
        res = supabase.table('memories') \
            .select('*') \
            .eq('id', memory_id) \
            .eq('is_current', True) \
            .maybe_single() \
            .execute()
        if not res or not res.data:
            return update_data
        current = res.data
        skip_keys = {'id', 'created_at', 'updated_at'}
        archived = {
            k: v for k, v in current.items()
            if k not in skip_keys
        }
        archived['is_current'] = False
        archived['version'] = current.get('version', 1)
        archived['supersedes_id'] = current.get('supersedes_id')
        arch_res = supabase.table('memories').insert(archived).execute()
        archived_id = arch_res.data[0]['id']
        update_data['version'] = current.get('version', 1) + 1
        update_data['supersedes_id'] = archived_id
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("db", "WARNING",
                       f"version_memory_for_update({memory_id}) failed: {e}")
    return update_data


def maybe_single_safe(builder):
    """Execute a builder chain with .limit(1).maybe_single() guard.

    Prevents the silent-null-on-multi-match failure mode of bare
    maybe_single(). Always caps the result set to 1 row before
    singularizing, so multiple matching rows return the first match
    instead of silently returning None.

    Usage:
        result = maybe_single_safe(
            supabase.table('people').select('id, name').eq('id', person_id)
        )
        if result.data:
            name = result.data['name']

    Args:
        builder: A Supabase query builder chain (e.g., from .table().select().eq()...)

    Returns:
        Same shape as builder.execute() — an object with .data attribute.
        .data is the row dict (exactly 1 match) or None (0 matches).
        Multiple matches are silently capped to the first row — consider
        adding explicit ordering if the first-match bias is wrong for
        your use case.
    """
    return builder.limit(1).maybe_single().execute()


def query_list_safe(builder, max_results=100):
    """Execute a query builder with an upper bound on results.

    Prevents unbounded result sets from queries that don't specify
    an explicit .limit(). Adds a cap if none is set by the caller.

    Usage:
        items = query_list_safe(
            supabase.table('tasks').select('id, title').eq('status', 'active'),
            max_results=50
        )
        for item in items.data or []:
            ...

    Args:
        builder: A Supabase query builder chain.
        max_results: Maximum number of rows to return (default 100).

    Returns:
        Same shape as builder.execute().
    """
    return builder.limit(max_results).execute()


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


