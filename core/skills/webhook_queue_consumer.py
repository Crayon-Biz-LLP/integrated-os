"""Async consumer for the pending_webhook_jobs queue.

Replaces the Vercel 55s timeout workaround with a reliable async queue.
The webhook endpoint returns immediately with HTTP 200; this consumer
processes the queued jobs and calls send_telegram to respond to the user.

Two consumers share responsibility:
  1. Dedicated endpoint  -> runs every 15s for fast responses
  2. Sentinel piggyback -> runs every 5min as a catch-all
"""

import json
from datetime import datetime, timezone
from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase

supabase = get_supabase()


async def process_pending_webhook_jobs(max_jobs: int = 5) -> dict:
    """Process pending webhook jobs with atomic claim + retry.

    Flow:
      1. SELECT up to max_jobs pending jobs, ordered by created_at ASC (FIFO).
      2. Atomically claim each job (UPDATE WHERE status='pending').
      3. Deserialize update_data and call process_webhook().
      4. Mark completed / failed.  Failed jobs retry up to 3 times before
         escalating to dead_letter.

    Args:
        max_jobs: Maximum number of jobs to process per call.
                  Keeps individual consumer runs fast (< 30s).

    Returns:
        dict with keys: processed (int), succeeded (int), failed (int)
    """
    try:
        rows = supabase.table("pending_webhook_jobs") \
            .select("id, update_data, retry_count") \
            .eq("status", "pending") \
            .order("created_at", desc=False) \
            .limit(max_jobs) \
            .execute()
    except Exception as e:
        audit_log_sync("webhook_queue", "WARNING", f"Fetch failed: {e}")
        return {"processed": 0, "succeeded": 0, "failed": 0}

    if not rows or not rows.data:
        return {"processed": 0, "succeeded": 0, "failed": 0}

    succeeded = 0
    failed = 0

    for job in rows.data:
        job_id = job["id"]

        # Atomic claim — only one consumer wins the race
        try:
            claim = supabase.table("pending_webhook_jobs") \
                .update({
                    "status": "processing",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }) \
                .eq("id", job_id) \
                .eq("status", "pending") \
                .execute()
            if not claim.data:
                continue  # Another consumer already claimed it
        except Exception as e:
            audit_log_sync("webhook_queue", "WARNING",
                           f"Claim failed for job {job_id}: {e}")
            continue

        # Deserialize the update_data
        try:
            raw = job["update_data"]
            if isinstance(raw, str):
                update_data = json.loads(raw)
            else:
                update_data = raw  # Already a dict (JSONB → Python dict)
        except (json.JSONDecodeError, TypeError) as e:
            audit_log_sync("webhook_queue", "ERROR",
                           f"Failed to parse update_data for job {job_id}: {e}")
            _mark_job(job_id, "dead_letter", error=f"Parse error: {e}")
            failed += 1
            continue

        # Process the webhook
        try:
            from core.webhook.handler import process_webhook
            from core.actions import begin_action_context, clear_action_context
            begin_action_context()
            try:
                await process_webhook(update_data)
            finally:
                clear_action_context()
            _mark_job(job_id, "completed")
            succeeded += 1
        except Exception as e:
            retry_count = (job.get("retry_count") or 0) + 1
            if retry_count >= 3:
                _mark_job(job_id, "dead_letter",
                          error=str(e)[:500])
                audit_log_sync("webhook_queue", "ERROR",
                               f"Job {job_id} escalated to dead_letter "
                               f"after {retry_count} attempts: {e}")
            else:
                _mark_job(job_id, "pending",
                          retry_count=retry_count, error=str(e)[:500])
                audit_log_sync("webhook_queue", "WARNING",
                               f"Job {job_id} failed (attempt {retry_count}/3): {e}")
            failed += 1

    return {"processed": len(rows.data), "succeeded": succeeded, "failed": failed}


def _mark_job(job_id: int, status: str, retry_count: int = None,
              error: str = None):
    """Update a job's status and metadata."""
    try:
        payload = {"status": status}
        if status == "completed":
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if retry_count is not None:
            payload["retry_count"] = retry_count
        if error is not None:
            payload["last_error"] = error[:500]

        supabase.table("pending_webhook_jobs") \
            .update(payload) \
            .eq("id", job_id) \
            .execute()
    except Exception as e:
        audit_log_sync("webhook_queue", "WARNING",
                       f"Failed to update job {job_id} → {status}: {e}")
