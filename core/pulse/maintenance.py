"""Maintenance tasks — data hygiene sweeps that actively FIX issues.

These tasks run periodically via sentinel piggybacks and fix DB problems
rather than just alerting about them. Each function is idempotent and
self-contained (no cross-file dependencies on other maintenance functions).

Sweeps restored (from the old architecture):
  1. run_raw_dump_cleanup()     — mark stale dumps >24h as abandoned
  2. run_graph_edge_expiry()    — mark 90-day-old graph edges as expired
  3. run_people_enrichment()    — enrich people table from graph edges
  4. run_weekly_housekeeping()  — stale tasks, pending nodes/edges, clarifications
  5. run_retry_failed_runs()    — retry failed retrieval index runs
"""

import json
from datetime import datetime, timezone, timedelta
from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync
from core.retrieval.config import config as retrieval_config
from core.retrieval.pipeline import process_pending_index_jobs, retry_failed_index_runs


async def run_index_queue(max_jobs: int = 3) -> int:
    """Process pending retrieval index jobs. Retries once if enabled."""
    if not retrieval_config.indexing_enabled:
        return 0
    indexed = 0
    try:
        indexed = await process_pending_index_jobs(max_jobs=max_jobs)
        if indexed > 0:
            audit_log_sync("maintenance", "INFO",
                           f"Index queue: {indexed} memory(ies) indexed")
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Index queue error: {e}")
    return indexed


async def run_retry_failed_runs(max_retries: int = 3, batch_size: int = 10) -> int:
    """Retry failed retrieval index runs."""
    if not retrieval_config.indexing_enabled:
        return 0
    retried = 0
    try:
        retried = await retry_failed_index_runs(
            max_retries=max_retries, batch_size=batch_size, retry_delay_seconds=0
        )
        if retried > 0:
            audit_log_sync("maintenance", "INFO",
                           f"Retry sweep: {retried} failed run(s) retried")
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Retry sweep error: {e}")
    return retried


def run_raw_dump_cleanup() -> int:
    """Mark stale staged/pending raw dumps >24h as abandoned."""
    supabase = tenant_aware_client()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        stale = supabase.table("raw_dumps") \
            .update({"status": "abandoned"}) \
            .in_("status", ["staged", "pending"]) \
            .lt("created_at", cutoff) \
            .execute()
        count = len(stale.data) if stale.data else 0
        if count > 0:
            audit_log_sync("maintenance", "INFO",
                           f"Raw dump cleanup: {count} stale dump(s) marked abandoned")
        return count
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Raw dump cleanup error: {e}")
        return 0


def run_graph_edge_expiry(expiry_days: int = 90) -> int:
    """Mark stale graph edges beyond expiry_days.

    Direct update on graph_edges: sets is_current=False for edges older than
    expiry_days where is_current=True. Falls back to inline UPDATE if the
    expire_stale_graph_edges RPC doesn't exist.
    """
    supabase = tenant_aware_client()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=expiry_days)).isoformat()
        # Try RPC first
        try:
            result = supabase.rpc("expire_stale_graph_edges", {"expiry_days": expiry_days}).execute()
            count = result.data if result.data else 0
        except Exception:
            # Fallback: inline UPDATE
            result = supabase.table("graph_edges") \
                .update({"is_current": False}) \
                .eq("is_current", True) \
                .lt("updated_at", cutoff) \
                .execute()
            count = len(result.data) if result.data else 0
        if count:
            audit_log_sync("maintenance", "INFO",
                           f"Graph edge expiry: {count} stale edge(s) marked")
        return count
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Graph edge expiry error: {e}")
        return 0


def run_weekly_housekeeping() -> dict:
    """Full weekly sweep — stale tasks, pending nodes/edges, clarifications.

    Idempotent: guarded by audit_log 20h dedup check.
    Returns summary dict.
    """
    supabase = tenant_aware_client()
    now = datetime.now(timezone.utc)

    try:
        # Check if already run in the last 20 hours
        last_run = supabase.table("audit_logs") \
            .select("id") \
            .eq("service", "maintenance") \
            .ilike("message", "%weekly_housekeeping%") \
            .gte("created_at", (now - timedelta(hours=20)).isoformat()) \
            .limit(1) \
            .execute()
        if last_run.data:
            return {"ran": False, "reason": "already_run_recently"}

        summary = {}

        # Stale tasks (>14 days, not done/cancelled)
        fourteen_days_ago = (now - timedelta(days=14)).isoformat()
        stale_tasks = supabase.table("tasks") \
            .select("id, title") \
            .eq("is_current", True) \
            .eq("status", "todo") \
            .lt("created_at", fourteen_days_ago) \
            .limit(10) \
            .execute()
        summary["stale_tasks"] = len(stale_tasks.data or [])

        # Unresolved clarifications
        clar = supabase.table("clarification_feedback") \
            .select("id") \
            .is_("resolved_at", "null") \
            .gt("expires_at", now.isoformat()) \
            .limit(10) \
            .execute()
        summary["unresolved_clarifications"] = len(clar.data or [])

        # Pending graph nodes
        pg = supabase.table("pending_nodes") \
            .select("id") \
            .eq("status", "pending") \
            .limit(10) \
            .execute()
        summary["pending_nodes"] = len(pg.data or [])

        # Pending graph edges
        pe = supabase.table("pending_graph_edges") \
            .select("id") \
            .eq("status", "pending") \
            .limit(10) \
            .execute()
        summary["pending_edges"] = len(pe.data or [])

        audit_log_sync("maintenance", "INFO",
                       f"weekly_housekeeping: {json.dumps(summary)}")
        return {"ran": True, "summary": summary}
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Weekly housekeeping error: {e}")
        return {"ran": False, "error": str(e)}
