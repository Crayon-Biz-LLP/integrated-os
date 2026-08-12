from core.lib.audit_logger import audit_log_sync


def reap_stuck_pulse_runs(supabase, pulse_type: str = "main", older_than_minutes: int = 30) -> int:
    """Mark pulse_runs rows stuck in 'running' beyond the age threshold as failed.

    A Modal function killed at its timeout never runs the except block, so
    complete_pulse_run() never fires and the row lies in 'running' forever
    (misleading health checks + a stuck run that looks active). Reap marks
    them failed with an explanatory error. Owner-scoped via the tenant
    facade when called under tenant_scope.

    Threshold 30 min: a legitimate run completes well under the 900s (15
    min) Modal timeout, so anything still 'running' past 30 min is dead.
    The 30-min heartbeat reaps the previous cycle's kill on the very next
    wake — no stale row survives more than one heartbeat.
    """
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        res = supabase.table("pulse_runs") \
            .select("id") \
            .eq("pulse_type", pulse_type) \
            .eq("status", "running") \
            .lt("started_at", cutoff) \
            .execute()
        ids = [r["id"] for r in (res.data or [])]
        for rid in ids:
            try:
                supabase.table("pulse_runs").update({
                    "status": "failed",
                    "error_message": f"reaped: stuck in 'running' > {older_than_minutes} min (timeout-killed)",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", rid).execute()
            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Reap pulse_run {rid} failed: {e}")
        if ids:
            audit_log_sync("pulse", "WARNING", f"Reaped {len(ids)} stuck pulse_run(s) in 'running'")
        return len(ids)
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Reap stuck pulse runs failed: {e}")
        return 0


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
