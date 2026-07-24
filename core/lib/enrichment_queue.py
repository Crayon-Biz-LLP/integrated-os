"""Enrichment Queue — replaces fire-and-forget with queue-based processing.

Fire-and-forget enrichment (asyncio.create_task) is killed when Vercel returns a
response. This module queues enrichment jobs synchronously during creation, then
processes them in the sentinel piggyback with atomic claim + retry.

Job types:
  task_graph   → write_graph_edges_for_task + extract_and_link_entities
  note_enrich  → extract_and_link_entities + get_embedding + metadata update
"""

import json
from datetime import datetime, timezone
from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()

MAX_RETRIES = 3


def enqueue_enrichment(
    job_type: str,
    target_type: str,
    target_id: int,
    content: str,
    related_id: str = None,
    related_org_id: str = None,
) -> bool:
    """Enqueue an enrichment job. Returns True if queued, False if skipped/duplicate.

    Replaces asyncio.create_task(_enrich_task_for_graph(...)) and
    asyncio.create_task(_enrich_note_for_graph(...)).

    Accepts related_org_id for creating task→organization BELONGS_TO edges.

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

        insert_data = {
            "job_type": job_type,
            "target_type": target_type,
            "target_id": target_id,
            "content": content,
            "related_id": related_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if related_org_id:
            insert_data["related_org_id"] = related_org_id

        supabase.table("pending_enrichment_jobs").insert(insert_data).execute()
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
        related_org_id = job.get("related_org_id")

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
                target_id=target_id, content=content, related_id=related_id, related_org_id=related_org_id
            )
        elif job_type == "note_enrich":
            success = await _process_note_enrichment(
                memory_id=target_id, content=content, source=related_id or "enrichment_queue",
                related_org_id=related_org_id,
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
    target_id: int, content: str, related_id: str = None, related_org_id: str = None
) -> bool:
    """Process a task_graph enrichment job: graph edges + entity extraction.

    Now creates task→org BELONGS_TO edge when related_org_id is provided.
    Also consumes entity extraction return values to backfill organization_id
    on the task if it was not set during creation.
    """
    try:
        from core.pulse.graph import write_graph_edges_for_task
        from core.pulse.entity_extractor import extract_and_link_entities

        # 1. Write graph edges — now includes task→org BELONGS_TO
        await write_graph_edges_for_task(
            task_id=target_id, task_title=content, project_id=related_id or "",
            organization_id=related_org_id
        )

        # 2. Entity extraction — CONSUME return values to backfill org_id
        org_candidates, proj_candidates = await extract_and_link_entities(
            content, target_id, "task"
        )

        # 3. If task was created without org_id but entity extraction found one, UPDATE it
        if org_candidates and not related_org_id:
            found_org_id = org_candidates[0]
            supabase = get_supabase()
            try:
                task_check = supabase.table('tasks').select('organization_id').eq('id', target_id).limit(1).execute()
                if task_check.data and task_check.data[0].get('organization_id') is None:
                    supabase.table('tasks').update({'organization_id': found_org_id}).eq('id', target_id).execute()
                    audit_log_sync(
                        "enrichment_queue", "INFO",
                        f"Backfilled organization_id={found_org_id} for task {target_id} from entity extraction"
                    )
            except Exception as fb_err:
                audit_log_sync(
                    "enrichment_queue", "WARNING",
                    f"Failed to backfill org_id for task {target_id}: {fb_err}"
                )

        return True
    except Exception as e:
        audit_log_sync(
            "enrichment_queue", "WARNING",
            f"task_graph enrichment failed for task {target_id}: {e}"
        )
        return False


async def _process_note_enrichment(
    memory_id: int, content: str, source: str, related_org_id: str = None
) -> bool:
    """Process a note_enrich enrichment job: entity extraction + embedding + metadata backfill.

    Updates the memory row with:
    - embedding vector (from get_embedding)
    - organization_id in metadata (from entity extraction results)
    - project_id in metadata (from entity extraction results)

    This is the second layer of defense (Bridge B):
    Layer 1: entity_linker.resolve_entities() runs at creation time in create_note_direct()
    Layer 2: Entity extraction in enrichment queue backfills any IDs still missing
    """
    try:
        from core.pulse.entity_extractor import extract_and_link_entities
        from core.llm import get_embedding

        # 1. Entity extraction — CONSUME return values (org_candidates, proj_candidates)
        org_candidates, proj_candidates = await extract_and_link_entities(
            content, memory_id, "memory"
        )

        # 2. Backfill organization_id if missing and entity extraction found one
        if org_candidates:
            found_org_id = org_candidates[0]
            # Check if note already has org_id in metadata
            try:
                mem_check = maybe_single_safe(
                    supabase.table('memories').select('metadata').eq('id', memory_id).eq('is_current', True)
                )
                if mem_check and mem_check.data:
                    current_meta = mem_check.data.get('metadata') or {}
                    if isinstance(current_meta, str):
                        try:
                            current_meta = json.loads(current_meta)
                        except Exception:
                            current_meta = {}
                    existing_org = current_meta.get('organization_id')
                    existing_proj = current_meta.get('project_id')

                    # Only backfill if not already set
                    updates = {}
                    if not existing_org:
                        current_meta['organization_id'] = found_org_id
                        updates['organization_id'] = found_org_id
                        audit_log_sync(
                            "enrichment_queue", "INFO",
                            f"Backfilled organization_id={found_org_id} for note {memory_id} from entity extraction"
                        )

                    if proj_candidates and not existing_proj:
                        found_proj = proj_candidates[0] if isinstance(proj_candidates[0], dict) else {'id': proj_candidates[0]}
                        found_proj_id = found_proj.get('id') if isinstance(found_proj, dict) else found_proj
                        if found_proj_id:
                            current_meta['project_id'] = found_proj_id
                            updates['project_id'] = found_proj_id
                            # Also backfill project's org_id if we found it
                            proj_org_id = found_proj.get('org_id') if isinstance(found_proj, dict) else None
                            if proj_org_id and not existing_org and not existing_org:
                                current_meta['organization_id'] = proj_org_id
                                updates['organization_id'] = proj_org_id
                            audit_log_sync(
                                "enrichment_queue", "INFO",
                                f"Backfilled project_id={found_proj_id} for note {memory_id} from entity extraction"
                            )

                    if updates:
                        col_updates = {'metadata': current_meta}
                        # Also update actual columns so queries on organization_id/project_id work
                        if 'organization_id' in updates:
                            col_updates['organization_id'] = updates['organization_id']
                        if 'project_id' in updates:
                            col_updates['project_id'] = updates['project_id']
                        supabase.table('memories').update(col_updates).eq('id', memory_id).eq('is_current', True).execute()
                        audit_log_sync(
                            "enrichment_queue", "INFO",
                            f"Backfilled note {memory_id}: columns + metadata: {updates}"
                        )
            except Exception as fb_err:
                audit_log_sync(
                    "enrichment_queue", "WARNING",
                    f"Failed to backfill note {memory_id} metadata: {fb_err}"
                )

        # 3. Embedding generation
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
