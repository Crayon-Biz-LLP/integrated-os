"""Standalone maintenance tasks — extracted from sentinel piggybacks.

These tasks run on their own schedule via a separate API endpoint and workflow,
independent of the sentinel. If sentinel fails, these critical data-hygiene
tasks still execute.

Piggybacks extracted (most critical first):
  1. Process pending retrieval index jobs
  2. Retry failed index runs
  3. Memory sweep (expired memories)
  4. Raw dump cleanup (stale dumps >24h)
  5. Orphan retrieval sweep
  6. Graph edge expiry (90-day stale)
  7. People enrichment from graph edges
  8. Weekly housekeeping sweep (stale tasks, pending nodes/edges, clarifications)
"""
from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.retrieval.config import config as retrieval_config
from core.retrieval.pipeline import process_pending_index_jobs, retry_failed_index_runs
from core.retrieval.cleanup import sweep_orphan_retrieval_entries, cleanup_memory_retrieval_index


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


def run_memory_sweep() -> int:
    """Delete expired memories and clean up their retrieval index."""
    supabase = get_supabase()
    try:
        expired = supabase.table("memories") \
            .select("id") \
            .lt("expires_at", datetime.now(timezone.utc).isoformat()) \
            .execute()
        expired_ids = [m["id"] for m in (expired.data or [])]
        if not expired_ids:
            audit_log_sync("maintenance", "INFO", "Memory sweep: no expired memories")
            return 0

        failed = 0
        for mid in expired_ids:
            ok = False
            for attempt in range(2):
                try:
                    cleanup_memory_retrieval_index(mid)
                    supabase.table("memories").delete().eq("id", mid).execute()
                    ok = True
                    break
                except Exception:
                    if attempt == 0:
                        continue
            if not ok:
                failed += 1
                audit_log_sync("maintenance", "WARNING",
                               f"Memory sweep: failed to clean memory {mid} after 2 attempts")

        if failed > len(expired_ids) // 2:
            audit_log_sync("maintenance", "WARNING",
                           f"Memory sweep: {failed}/{len(expired_ids)} items failed cleanup")
        audit_log_sync("maintenance", "INFO",
                       f"Memory sweep: {len(expired_ids) - failed}/{len(expired_ids)} expired memory(ies) removed")

        # Also run orphan sweep after cleanup
        try:
            sweep_orphan_retrieval_entries()
        except Exception:
            pass
        return len(expired_ids) - failed
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Memory sweep error: {e}")
        return 0


def run_raw_dump_cleanup() -> int:
    """Mark stale staged/pending raw dumps >24h as abandoned."""
    supabase = get_supabase()
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


def run_orphan_sweep() -> int:
    """Sweep orphaned retrieval entries."""
    try:
        sweep_orphan_retrieval_entries()
        audit_log_sync("maintenance", "INFO", "Orphan sweep: completed")
        return 1
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Orphan sweep error: {e}")
        return 0


def run_graph_edge_expiry(expiry_days: int = 90) -> int:
    """Mark stale graph edges beyond expiry_days."""
    supabase = get_supabase()
    try:
        result = supabase.rpc("expire_stale_graph_edges", {"expiry_days": expiry_days}).execute()
        count = result.data if result.data else 0
        if count:
            audit_log_sync("maintenance", "INFO",
                           f"Graph edge expiry: {count} stale edge(s) marked")
        return count
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Graph edge expiry error: {e}")
        return 0


def run_people_enrichment() -> int:
    """Enrich people table from graph edges."""
    try:
        from core.lib.people_utils import enrich_people_from_graph
        enriched = enrich_people_from_graph()
        if enriched:
            audit_log_sync("maintenance", "INFO",
                           f"People enrichment: {enriched} person(s) updated")
        return enriched
    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"People enrichment error: {e}")
        return 0


def run_weekly_housekeeping() -> dict:
    """Full weekly sweep — stale tasks, pending nodes/edges, clarifications.

    Idempotent: guarded by audit_log 20h dedup check.
    Returns summary dict.
    """
    supabase = get_supabase()
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
        pg = supabase.table("pending_graph_nodes") \
            .select("id") \
            .eq("status", "pending") \
            .limit(10) \
            .execute()
        summary["pending_graph_nodes"] = len(pg.data or [])

        # Pending graph edges
        pe = supabase.table("pending_graph_edges") \
            .select("id") \
            .eq("status", "pending") \
            .limit(10) \
            .execute()
        summary["pending_graph_edges"] = len(pe.data or [])

        # Expire stale decisions
        from core.decisions import expire_stale_decisions
        expired_decisions = expire_stale_decisions()
        summary["expired_decisions"] = expired_decisions

        audit_log_sync("maintenance", "INFO",
                       f"weekly_housekeeping: {summary}")
        return {"ran": True, "summary": summary}

    except Exception as e:
        audit_log_sync("maintenance", "WARNING", f"Weekly housekeeping error: {e}")
        return {"ran": False, "error": str(e)}


async def process_maintenance(mode: str = "standard") -> dict:
    """Orchestrate all maintenance tasks.

    Modes:
      - "standard": index queue + raw dump cleanup + orphan sweep (every ~15 min)
      - "daily": adds memory sweep + graph edge expiry + people enrichment
      - "weekly": adds full housekeeping sweep
    """
    results = {"mode": mode}

    # Always run: index queue, raw dump cleanup, orphan sweep
    results["index_queue"] = await run_index_queue(max_jobs=3)
    results["raw_dump_cleanup"] = run_raw_dump_cleanup()
    results["orphan_sweep"] = run_orphan_sweep()

    if mode in ("daily", "weekly"):
        results["retry_failed_runs"] = await run_retry_failed_runs()
        results["memory_sweep"] = run_memory_sweep()
        results["graph_edge_expiry"] = run_graph_edge_expiry()
        results["people_enrichment"] = run_people_enrichment()

    if mode == "weekly":
        results["weekly_housekeeping"] = run_weekly_housekeeping()

    # Count actionable results (int values are counts, bool True marks completion)
    action_count = sum(v for v in results.values() if isinstance(v, int) or v is True)
    results["total_actions"] = action_count
    audit_log_sync("maintenance", "INFO",
                   f"Maintenance run ({mode}): {results}")
    return results
