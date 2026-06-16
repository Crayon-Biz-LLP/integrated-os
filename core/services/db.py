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
            .update({"status": "pending"}) \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("db", "WARNING", f"Zombie recovery failed: {e}")


def versioned_update(table_name: str, record_id: int, update_data: dict, user_id=None, change_source=None, change_reason=None):
    supabase = get_supabase()
    from core.lib.audit_logger import audit_log_sync
    try:
        current = supabase.table(table_name).select('*').eq('id', record_id).execute()
        if not current.data:
            audit_log_sync("db", "WARNING", f"Record {record_id} not found in {table_name}")
            return False

        old_record = current.data[0]
        
        # Concurrency guard: if old_record is no longer current, it was superseded by another process
        if not old_record.get('is_current', True):
            raise Exception(f"Concurrency conflict: Record {record_id} is no longer current.")

        new_record = {
            **{k: v for k, v in old_record.items()
               if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id', 'updated_at']},
            **{k: v for k, v in update_data.items() if v is not None},
            **{k: v for k, v in update_data.items() if v is None},
            'is_current': True
        }

        old_version = old_record.get('version', 0) or 0
        new_record['version'] = old_version + 1
        new_record['supersedes_id'] = record_id

        result = supabase.table(table_name).insert(new_record).execute()
        
        try:
            supabase.table(table_name).update({"is_current": False}).eq('id', record_id).execute()
        except Exception as update_err:
            if result.data and len(result.data) > 0:
                new_id = result.data[0].get('id')
                if new_id:
                    # Rollback the insert to prevent duplicate is_current=True records
                    try:
                        supabase.table(table_name).delete().eq('id', new_id).execute()
                    except Exception as del_err:
                        audit_log_sync("db", "CRITICAL", f"Failed to rollback version insert {new_id}: {del_err}. You now have duplicate is_current=True records!")
                        # Try to soft-delete the new one as a desperate measure
                        try:
                            supabase.table(table_name).update({"is_current": False}).eq('id', new_id).execute()
                        except Exception:
                            pass
            raise update_err

        if change_source or change_reason:
            audit_log_sync("db", "INFO",
                          f"Versioned update: {table_name}:{record_id} v{new_record['version']}",
                          {"source": change_source, "reason": change_reason, "user_id": user_id})

        return bool(result.data)

    except Exception as e:
        audit_log_sync("db", "WARNING", f"Versioned update failed for {table_name}:{record_id}, falling back: {e}")
        fallback_data = {**update_data, 'is_current': True}
        supabase.table(table_name).update(fallback_data).eq('id', record_id).execute()
        return True
