from core.lib.audit_logger import audit_log_sync

async def create_pulse_run(supabase, pulse_type: str, trigger: str) -> int | None:
    try:
        res = supabase.table("pulse_runs").insert({
            "pulse_type": pulse_type,
            "trigger": trigger,
            "status": "running",
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to create pulse_run: {e}")
        return None

async def complete_pulse_run(supabase, run_id: int, *,
    status="completed", dumps_processed=None, tasks_created=None,
    error_message=None, metadata=None):
    if not run_id:
        return
    try:
        update = {"status": status}
        if status == "completed":
            from datetime import datetime, timezone
            update["completed_at"] = datetime.now(timezone.utc).isoformat()
        elif status == "failed":
            from datetime import datetime, timezone
            update["failed_at"] = datetime.now(timezone.utc).isoformat()
        if dumps_processed is not None:
            update["dumps_processed"] = dumps_processed
        if tasks_created is not None:
            update["tasks_created"] = tasks_created
        if error_message is not None:
            update["error_message"] = str(error_message)[:500]
        if metadata is not None:
            update["metadata"] = metadata
        supabase.table("pulse_runs").update(update).eq("id", run_id).execute()
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Failed to complete pulse_run {run_id}: {e}")
