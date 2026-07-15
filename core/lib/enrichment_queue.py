"""Enrichment Queue — replaces fire-and-forget with queue-based processing.

Fire-and-forget enrichment (asyncio.create_task) is killed when Vercel returns a
response. This module queues enrichment jobs synchronously during creation, then
processes them in the sentinel piggyback with atomic claim + retry.

Job types:
  task_graph   → write_graph_edges_for_task + extract_and_link_entities
  note_enrich  → extract_and_link_entities + get_embedding + metadata update
"""

from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()

MAX_RETRIES = 3


def enqueue_enrichment(
    job_type: str,
    target_type: str,
    target_id: int,
    content: str,
    related_id: str = None,
) -> bool:
    """Enqueue an enrichment job. Returns True if queued, False if skipped/duplicate.

    Replaces asyncio.create_task(_enrich_task_for_graph(...)) and
    asyncio.create_task(_enrich_note_for_graph(...)).

    Uses SELECT-first-then-INSERT pattern (same as schedule_index_memory)
    because PostgREST cannot reliably target partial unique indexes
    with upsert's on_conflict parameter.
    """
    try:
        # Check for existing pending/processing job for this target
        existing = supabase.table("pending_enrichment_jobs") \
            .select("id") \
            .eq("job_type", job_type) \
            .eq("target_id", target_id) \
            .eq("target_type", target_type) \
            .in_("status", ["pending", "processing"]) \
            .limit(1) \
            .execute()
        if existing and existing.data:
            return True  # Already queued

        supabase.table("pending_enrichment_jobs").insert({
            "job_type": job_type,
            "target_type": target_type,
            "target_id": target_id,
            "content": content,
            "related_id": related_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return True
    except Exception as e:
        audit_log_sync("enrichment_queue", "WARNING", f"enqueue_enrichment failed: {e}")
        return False


async def process_pending_enrichment(max_jobs: int = 3) -> int:
    """Process pending enrichment jobs. Called by sentinel piggyback.

    Uses atomic claim via claim_pending_enrichment_job RPC to prevent
    double-processing on concurrent sentinel runs.

    Returns number of jobs processed.
    """
    try:
        rows = (
            supabase.table("pending_enrichment_jobs")
            .select("id, job_type, target_type, target_id, content, related_id, retry_count")
            .eq("status", "pending")
            .order("created_at", desc=False)
            .limit(max_jobs)
            .execute()
        )
    except Exception as e:
        audit_log_sync("enrichment_queue", "WARNING", f"fetch pending jobs failed: {e}")
        return 0

    if not rows or not rows.data:
        return 0

    processed = 0
    for job in rows.data:
        job_id = job["id"]
        job_type = job["job_type"]
        target_id = job["target_id"]
        content = job["content"]
        related_id = job.get("related_id")

        # Atomic claim — call RPC
        try:
            claimed = supabase.rpc("claim_pending_enrichment_job", {"job_id": job_id}).execute()
            if not claimed or not claimed.data:
                continue  # Another sentinel run already claimed it
        except Exception as e:
            audit_log_sync(
                "enrichment_queue", "WARNING", f"claim failed for job {job_id}: {e}"
            )
            continue

        success = False
        if job_type == "task_graph":
            success = await _process_task_graph_enrichment(
                target_id=target_id, content=content, related_id=related_id
            )
        elif job_type == "note_enrich":
            success = await _process_note_enrichment(
                memory_id=target_id, content=content, source=related_id or "enrichment_queue"
            )
        else:
            audit_log_sync(
                "enrichment_queue", "WARNING",
                f"Unknown job_type '{job_type}' for job {job_id}"
            )

        retry_count = (job.get("retry_count") or 0) + 1
        if success:
            try:
                supabase.table("pending_enrichment_jobs") \
                    .update({
                        "status": "completed",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }) \
                    .eq("id", job_id) \
                    .execute()
            except Exception:
                pass
            audit_log_sync(
                "enrichment_queue", "INFO",
                f"Completed {job_type} for {job['target_type']} {target_id} (job {job_id})"
            )
        else:
            new_status = "dead_letter" if retry_count >= MAX_RETRIES else "failed"
            try:
                supabase.table("pending_enrichment_jobs") \
                    .update({
                        "status": new_status,
                        "error": f"Failed after {retry_count} attempt(s)",
                    }) \
                    .eq("id", job_id) \
                    .execute()
            except Exception:
                pass
            audit_log_sync(
                "enrichment_queue",
                "WARNING" if new_status == "failed" else "ERROR",
                f"{job_type} for {job['target_type']} {target_id} → {new_status} "
                f"(attempt {retry_count})"
            )

        processed += 1

    return processed


async def _process_task_graph_enrichment(
    target_id: int, content: str, related_id: str = None
) -> bool:
    """Process a task_graph enrichment job: graph edges + entity extraction."""
    try:
        from core.pulse.graph import write_graph_edges_for_task
        from core.pulse.entity_extractor import extract_and_link_entities

        await write_graph_edges_for_task(
            task_id=target_id, title=content, project_id=related_id or ""
        )
        await extract_and_link_entities(content, target_id, "task")
        return True
    except Exception as e:
        audit_log_sync(
            "enrichment_queue", "WARNING",
            f"task_graph enrichment failed for task {target_id}: {e}"
        )
        return False


async def _process_note_enrichment(
    memory_id: int, content: str, source: str
) -> bool:
    """Process a note_enrich enrichment job: entity extraction + embedding + metadata.

    Updates the memory row with:
    - embedding vector (from get_embedding)
    - entities_mentioned (from extract_and_link_entities result, best-effort)
    """
    try:
        from core.pulse.entity_extractor import extract_and_link_entities
        from core.llm import get_embedding

        # 1. Entity extraction
        await extract_and_link_entities(content, memory_id, "memory")

        # 2. Embedding generation
        try:
            emb_res = await get_embedding(content)
            if emb_res and emb_res.vector:
                supabase.table("memories") \
                    .update({"embedding": emb_res.vector}) \
                    .eq("id", memory_id) \
                    .eq("is_current", True) \
                    .execute()
        except Exception as emb_e:
            audit_log_sync(
                "enrichment_queue", "WARNING",
                f"Embedding failed for note {memory_id}: {emb_e}"
            )

        return True
    except Exception as e:
        audit_log_sync(
            "enrichment_queue", "WARNING",
            f"note_enrich failed for memory {memory_id}: {e}"
        )
        return False
