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
from core.services.db import maybe_single_safe, tenant_aware_client
from core.lib.audit_logger import audit_log_sync

supabase = tenant_aware_client()

MAX_RETRIES = 3


def enqueue_enrichment(
    job_type: str,
    target_type: str,
    target_id: int,
    content: str,
    related_id: str = None,
    related_org_id: str = None,
    full_text: str = None,           # NEW: original message text
    pending_org_id: int = None,      # NEW: pending org from EntityContext
    entity_context: dict = None,     # NEW: serialized EntityContext
) -> bool:
    """Enqueue an enrichment job. Returns True if queued, False if skipped/duplicate.

    Accepts entity_context from extract_context_from_source() to avoid
    re-extracting entities in the enrichment queue.

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
        if full_text:
            insert_data["full_text"] = full_text
        if pending_org_id:
            insert_data["pending_org_id"] = pending_org_id
        if entity_context:
            insert_data["entity_context"] = json.dumps(entity_context)

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
            .select("id, job_type, target_type, target_id, content, related_id, retry_count, "
                    "related_org_id, full_text, pending_org_id, entity_context")
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
        full_text = job.get("full_text")
        pending_org_id = job.get("pending_org_id")
        entity_context_raw = job.get("entity_context")

        # Parse entity_context from JSONB
        entity_context = None
        if entity_context_raw:
            try:
                entity_context = json.loads(entity_context_raw) if isinstance(entity_context_raw, str) else entity_context_raw
            except Exception:
                entity_context = None

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
                target_id=target_id, content=content, related_id=related_id,
                related_org_id=related_org_id, full_text=full_text,
                pending_org_id=pending_org_id, entity_context=entity_context,
            )
        elif job_type == "note_enrich":
            success = await _process_note_enrichment(
                memory_id=target_id, content=content, source=related_id or "enrichment_queue",
                related_org_id=related_org_id, full_text=full_text,
                pending_org_id=pending_org_id, entity_context=entity_context,
            )
        elif job_type == "doc_enrich":
            success = await _process_doc_enrichment(
                document_id=target_id, content=content
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
    target_id: int, content: str, related_id: str = None, related_org_id: str = None,
    full_text: str = None, pending_org_id: int = None, entity_context: dict = None,
) -> bool:
    """Process a task_graph enrichment job: graph edges + entity linkage.

    Uses pre-extracted EntityContext when available (no re-extraction).
    Creates task→org BELONGS_TO edge (and task→person INVOLVES edges via
    write_graph_edges_for_task's known-person scan). Never creates pending
    rows of its own — pending edges for unconfirmed persons are created only
    at decision-gated approval sites (HITL).
    """
    # --- PREVENTION GUARD ---
    if content and ('[TEST]' in content or content in ['Valid Event', 'Test Event', 'Test Note', 'Test Note for Enrichment']):
        audit_log_sync("enrichment_queue", "INFO", f"Skipping graph extraction for test task {target_id}")
        return True

    try:
        from core.pulse.graph import write_graph_edges_for_task

        # 1. Write graph edges — task→org BELONGS_TO
        org_id_for_edges = related_org_id
        if not org_id_for_edges and entity_context:
            org_id_for_edges = entity_context.get("organization_id")

        await write_graph_edges_for_task(
            task_id=target_id, task_title=content,
            organization_id=org_id_for_edges
        )

        # 2. (Removed) Person→task INVOLVES edge proposals from EntityContext.
        # Historically this created pending edges for unconfirmed persons from
        # the sentinel background job — ungated HITL rows the user never asked
        # for. Known persons are covered by write_graph_edges_for_task's
        # known-person scan above; new persons get edges at approval (Bridge C).

        # 3. Entity extraction — use extract_context_from_source with full text
        if entity_context:
            # Context already extracted at creation time — skip LLM call
            audit_log_sync("enrichment_queue", "INFO",
                f"Skipped entity extraction for task {target_id} — context pre-extracted")
        else:
            # Backward compat: extract with full text (not just title)
            from core.lib.entity_context import extract_context_from_source
            fallback_ctx = await extract_context_from_source(
                full_text or content, timing="async"
            )

            # Backfill organization_id if extraction found one
            if fallback_ctx.organization_id and not related_org_id and not pending_org_id:
                supabase = tenant_aware_client()
                try:
                    task_check = supabase.table('tasks').select('organization_id').eq('id', target_id).limit(1).execute()
                    if task_check.data and task_check.data[0].get('organization_id') is None:
                        supabase.table('tasks').update({'organization_id': fallback_ctx.organization_id}).eq('id', target_id).execute()
                        audit_log_sync(
                            "enrichment_queue", "INFO",
                            f"Backfilled organization_id={fallback_ctx.organization_id} for task {target_id}"
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
    memory_id: int, content: str, source: str, related_org_id: str = None,
    full_text: str = None, pending_org_id: int = None, entity_context: dict = None,
) -> bool:
    """Process a note_enrich enrichment job: entity linkage + embedding + metadata backfill.

    Uses pre-extracted EntityContext when available (no re-extraction).
    Falls back to extract_and_link_entities for backward compatibility.
    """
    # --- PREVENTION GUARD ---
    if content and ('[TEST]' in content or content in ['Valid Event', 'Test Event', 'Test Note', 'Test Note for Enrichment']):
        audit_log_sync("enrichment_queue", "INFO", f"Skipping graph extraction for test note {memory_id}")
        return True

    try:
        from core.llm import get_embedding

        # 1. Entity linkage — use EntityContext if available, otherwise extract
        if entity_context:
            # Context already extracted at creation time — skip LLM call
            found_org_id = entity_context.get("organization_id") or related_org_id
            audit_log_sync("enrichment_queue", "INFO",
                f"Skipped entity extraction for note {memory_id} — context pre-extracted")
        else:
            # Backward compat: extract with full text using the single extraction pipeline
            from core.lib.entity_context import extract_context_from_source
            fallback_ctx = await extract_context_from_source(
                full_text or content, timing="async"
            )
            found_org_id = fallback_ctx.organization_id or related_org_id

        # 2. Backfill organization_id if missing
        if found_org_id:
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

                    if not existing_org:
                        current_meta['organization_id'] = found_org_id
                        supabase.table('memories').update({
                            'metadata': current_meta,
                            'organization_id': found_org_id,
                        }).eq('id', memory_id).eq('is_current', True).execute()
                        audit_log_sync(
                            "enrichment_queue", "INFO",
                            f"Backfilled organization_id={found_org_id} for note {memory_id}"
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

async def _process_doc_enrichment(document_id: int, content: str) -> bool:
    """Process a doc_enrich enrichment job: entity extraction on full document body.

    Extracts entities from the document and records them as a 'raw_dump' source_type,
    putting the document's org/people references directly into the graph.
    """
    # --- PREVENTION GUARD ---
    if content and ('[TEST]' in content or content in ['Valid Event', 'Test Event', 'Test Note', 'Test Note for Enrichment']):
        audit_log_sync("enrichment_queue", "INFO", f"Skipping graph extraction for test document {document_id}")
        return True

    try:
        from core.lib.entity_context import extract_context_from_source

        # 1. Entity extraction — use single pipeline with full text
        ctx = await extract_context_from_source(content, timing="async")
        # Stamp org on document if found
        if ctx.organization_id:
            supabase.table('document_items').update({'organization_id': ctx.organization_id}).eq('id', document_id).execute()
        elif ctx.pending_org_id:
            supabase.table('document_items').update({'pending_org_id': ctx.pending_org_id}).eq('id', document_id).execute()
        return True
    except Exception as e:
        audit_log_sync(
            "enrichment_queue", "WARNING",
            f"doc_enrich failed for document {document_id}: {e}"
        )
        return False
