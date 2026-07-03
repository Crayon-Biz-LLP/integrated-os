from typing import Optional, List, Tuple
import asyncio
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.llm import get_embedding
from core.retrieval.config import config, INDEX_VERSION, BACKFILL_MAX_CONCURRENCY
from core.retrieval.chunker import chunk_text, compute_fingerprint
from core.retrieval.extractor import extract_triples
from core.retrieval.graph import (
    build_triple_graph, upsert_memory_bundle_link,
    update_node_stats,
)
from core.retrieval.schema import Passage

supabase = get_supabase()

# Module-level concurrency limiter for extraction — shared across all index_memory() calls
index_semaphore = asyncio.Semaphore(BACKFILL_MAX_CONCURRENCY)


async def index_memory(memory_id: int, content: str, memory_type: str,
                       source: str, metadata: Optional[dict] = None) -> bool:
    """Index a single memory item into the retrieval substrate.

    Steps:
    1. Check/fingerprint for idempotent skip.
    2. Create index run record.
    3. Chunk into passages.
    4. Embed and upsert each passage.
    5. Extract triples (rate-limited by module-level semaphore).
    6. Build phrase nodes and edges.
    7. Link passages to memory bundle.
    8. Mark index run completed or partial/failed.
    """
    if not config.indexing_enabled:
        return False

    source_type = memory_type or "memory"
    source_id = str(memory_id)
    fp = compute_fingerprint(content)

    existing = supabase.table("retrieval_index_runs") \
        .select("id, status") \
        .eq("source_type", source_type) \
        .eq("source_id", source_id) \
        .eq("source_fingerprint", fp) \
        .eq("index_version", INDEX_VERSION) \
        .maybe_single() \
        .execute()

    if existing and existing.data and existing.data.get("status") == "completed":
        return True

    # Content changed or previous run failed/partial — clean up old passages
    # for this source so we don't orphan data from the last index
    try:
        supabase.table("retrieval_passages") \
            .delete() \
            .eq("source_type", source_type) \
            .eq("source_id", source_id) \
            .eq("index_version", INDEX_VERSION) \
            .execute()
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Passage cleanup failed for {source_type}/{source_id}: {e}")
        # Non-fatal — upsert will still work, old passages may linger

    run_id = None
    try:
        run_res = supabase.table("retrieval_index_runs") \
            .upsert({
                "source_type": source_type,
                "source_id": source_id,
                "source_fingerprint": fp,
                "index_version": INDEX_VERSION,
                "status": "processing",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="source_type,source_id,index_version") \
            .execute()
        if run_res and run_res.data:
            run_id = run_res.data[0]["id"]

        passages = chunk_text(content, source_type, source_id,
                               memory_id=memory_id, index_version=INDEX_VERSION)

        if not passages:
            _set_run_status(run_id, "completed")
            return True

        # Phase 1: Embed all passages in parallel
        embed_tasks = [_upsert_passage(p) for p in passages]
        embed_results = await asyncio.gather(*embed_tasks)
        inserted_passages = [
            (p_id, p) for p_id, p in zip(embed_results, passages) if p_id
        ]

        for p_id, p in inserted_passages:
            if memory_id:
                await upsert_memory_bundle_link(memory_id, p_id)

        async def extract_passage(p_id: int, p: Passage) -> Tuple[bool, bool, list]:
            """Returns (had_triples, llm_ok, entity_labels)."""
            async with index_semaphore:
                triples, llm_ok = await extract_triples(
                    text=p.text,
                    source_type=source_type,
                    source_id=source_id,
                    passage_id=p_id,
                    index_version=INDEX_VERSION,
                )
                if not llm_ok or not triples:
                    return bool(triples), llm_ok, []
                await build_triple_graph(triples, p_id, source_type, source_id)
                if config.chunk_enrichment and triples:
                    entity_labels = list(dict.fromkeys(
                        [t.normalized_subject for t in triples]
                        + [t.normalized_object for t in triples]
                    ))[:3]
                    return True, True, entity_labels
                return True, True, []

        # Phase 2: Extract triples (inside semaphore, rate-limited at 39 RPM)
        extract_tasks = [extract_passage(p_id, p) for p_id, p in inserted_passages]
        extract_results: List[Tuple[bool, bool, list]] = await asyncio.gather(*extract_tasks)

        # Phase 3: Re-embed with entity labels (outside semaphore)
        reembed_tasks = [
            reembed_passage_with_entities(
                passage_id=p_id, raw_text=p.text, entity_labels=el,
            )
            for (p_id, p), (_, _, el) in zip(inserted_passages, extract_results)
            if el
        ]
        if reembed_tasks:
            await asyncio.gather(*reembed_tasks)

        any_failure = any(not r[1] for r in extract_results)
        any_success = any(r[1] for r in extract_results)

        if not any_success:
            _set_run_status(run_id, "failed",
                            error="All passage extractions failed (LLM errors)")
            return False

        if any_failure:
            _set_run_status(run_id, "completed_partial",
                            error="Some passage extractions failed (LLM errors)")
            audit_log_sync("retrieval", "WARNING",
                           f"Index {run_id} for {source_type}/{source_id} completed partial: "
                           f"{sum(1 for r in extract_results if not r[1])}/{len(extract_results)} passages failed")
        else:
            _set_run_status(run_id, "completed")

        return True

    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"Index failed for {source_type}/{source_id}: {e}")
        _set_run_status(run_id, "failed", error=str(e)[:500])
        return False


def _build_enrichment_prefix(source_type: str, entity_labels: list) -> str:
    """Build a short, stable metadata prefix for embedding enrichment.

    Format: `[source_type, entity1, entity2, entity3]`
    Keeps prefix under ~80 chars. Deduplicates and limits to top-3 entities.
    Used for both passage indexing and query embedding to keep spaces aligned.
    """
    parts = [source_type]
    seen = set()
    for label in entity_labels:
        clean = label.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            parts.append(clean)
        if len(parts) >= 4:  # source_type + 3 entities max
            break
    return "[" + ", ".join(parts) + "]"


async def reembed_passage_with_entities(
    passage_id: int, raw_text: str, entity_labels: list
) -> bool:
    """Re-embed a passage with entity labels prepended.

    Called after triple extraction to enrich the embedding with entity context.
    Updates the passage's embedding and text columns in-place.
    The raw_text is preserved in the raw_text column.
    """
    if not entity_labels:
        return True  # Nothing to enrich

    # Use generic "retrieval" prefix to align with query-side embedding space
    prefix = _build_enrichment_prefix("retrieval", entity_labels)
    enriched_text = f"{prefix} {raw_text}"

    try:
        emb_res = await get_embedding(enriched_text)
        if not emb_res or not emb_res.vector:
            return False

        supabase.table("retrieval_passages") \
            .update({
                "text": enriched_text,
                "embedding": emb_res.vector,
            }) \
            .eq("id", passage_id) \
            .execute()
        return True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"reembed_passage_with_entities failed for passage {passage_id}: {e}")
        return False


async def _upsert_passage(passage: Passage) -> Optional[int]:
    """Upsert a passage, return its ID.
    
    When RETRIEVAL_CHUNK_ENRICHMENT is enabled, stores raw user text in `raw_text`
    and embeds `[source_type] text` (source-type-enriched) in `text` + `embedding`.
    After entity extraction, reembed_passage_with_entities upgrades to
    `[source_type, entity1, entity2] text`.
    """
    try:
        existing = supabase.table("retrieval_passages") \
            .select("id") \
            .eq("source_fingerprint", passage.source_fingerprint) \
            .eq("passage_index", passage.passage_index) \
            .eq("index_version", passage.index_version) \
            .maybe_single() \
            .execute()

        if existing and existing.data:
            return existing.data["id"]

        # Determine what to embed and what to display
        if config.chunk_enrichment:
            raw_text = passage.text
            enriched_text = _build_enrichment_prefix("retrieval", []) + " " + raw_text
        else:
            raw_text = passage.text
            enriched_text = passage.text

        emb_res = await get_embedding(enriched_text)
        if not emb_res or not emb_res.vector:
            audit_log_sync("retrieval", "WARNING",
                           f"Embedding returned None for passage {passage.passage_index} "
                           f"({passage.source_type}/{passage.source_id})")

        row = {
            "source_type": passage.source_type,
            "source_id": passage.source_id,
            "memory_id": passage.memory_id,
            "passage_index": passage.passage_index,
            "text": enriched_text,
            "raw_text": raw_text,
            "char_count": passage.char_count,
            "embedding": emb_res.vector if emb_res else None,
            "source_fingerprint": passage.source_fingerprint,
            "index_version": passage.index_version,
            "metadata": passage.metadata,
        }
        result = supabase.table("retrieval_passages") \
            .insert(row) \
            .execute()

        if result and result.data:
            return result.data[0]["id"]
        return None

    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"upsert_passage failed for {passage.source_type}/{passage.source_id} "
                       f"passage {passage.passage_index}: {e}")
        return None


def _set_run_status(run_id: Optional[int], status: str, error: Optional[str] = None):
    """Update index run status with audit logging."""
    if not run_id:
        return
    try:
        payload = {
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            payload["error"] = error[:500]
        supabase.table("retrieval_index_runs") \
            .update(payload) \
            .eq("id", run_id) \
            .execute()
        audit_log_sync("retrieval", "INFO",
                       f"Index run {run_id} → {status}"
                       + (f": {error[:200]}" if error else ""))
    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"Failed to update index run {run_id} status to {status}: {e}")


async def retry_failed_index_runs(max_retries: int = 3,
                                   batch_size: int = 20,
                                   retry_delay_seconds: int = 10) -> int:
    """Sweep failed index runs and retry them.

    Process:
    1. Fetch up to batch_size runs with status='failed' AND retry_count < max_retries.
    2. Set status='retrying', increment retry_count.
    3. Call index_memory() for each source.
    4. On success → status='completed'.
    5. On failure after max_retries → status='dead_letter'.

    Returns number of runs retried.
    """
    failed = supabase.table("retrieval_index_runs") \
        .select("id, source_type, source_id") \
        .eq("status", "failed") \
        .lt("retry_count", max_retries) \
        .limit(batch_size) \
        .execute()

    if not failed or not failed.data:
        audit_log_sync("retrieval", "INFO", "Retry sweeper: no failed runs to retry")
        return 0

    runs = failed.data
    audit_log_sync("retrieval", "INFO",
                   f"Retry sweeper: found {len(runs)} failed runs to retry")

    retried = 0
    for run in runs:
        try:
            # Fetch current retry_count and increment
            current = supabase.table("retrieval_index_runs") \
                .select("retry_count") \
                .eq("id", run["id"]) \
                .maybe_single() \
                .execute()
            cur_count = (current.data.get("retry_count", 0)
                         if current and current.data else 0)

            supabase.table("retrieval_index_runs") \
                .update({
                    "status": "retrying",
                    "retry_count": cur_count + 1,
                }) \
                .eq("id", run["id"]) \
                .execute()

            audit_log_sync("retrieval", "INFO",
                           f"Retrying index run {run['id']} "
                           f"({run['source_type']}/{run['source_id']})")

            # Fetch canonical memory content for re-indexing
            mem = supabase.table("memories") \
                .select("content, memory_type") \
                .eq("id", int(run["source_id"])) \
                .maybe_single() \
                .execute()

            if not mem or not mem.data:
                audit_log_sync("retrieval", "WARNING",
                               f"Retry sweeper: memory {run['source_id']} not found, "
                               f"marking dead_letter")
                supabase.table("retrieval_index_runs") \
                    .update({"status": "dead_letter", "error": "Source memory deleted"}) \
                    .eq("id", run["id"]) \
                    .execute()
                retried += 1
                continue

            success = await index_memory(
                memory_id=int(run["source_id"]),
                content=mem.data["content"],
                memory_type=mem.data.get("memory_type", run["source_type"]),
                source="retry-sweeper",
            )

            if not success:
                failed_again = supabase.table("retrieval_index_runs") \
                    .select("retry_count") \
                    .eq("id", run["id"]) \
                    .maybe_single() \
                    .execute()
                count = (failed_again.data.get("retry_count", 0)
                         if failed_again and failed_again.data else 0)

                if count >= max_retries:
                    supabase.table("retrieval_index_runs") \
                        .update({"status": "dead_letter"}) \
                        .eq("id", run["id"]) \
                        .execute()
                    audit_log_sync("retrieval", "WARNING",
                                   f"Index run {run['id']} escalated to dead_letter "
                                   f"after {max_retries} retries")
            retried += 1

        except Exception as e:
            audit_log_sync("retrieval", "ERROR",
                           f"Retry sweeper failed for run {run['id']}: {e}")
            _set_run_status(run["id"], "failed", error=f"retry-sweeper error: {str(e)[:200]}")

        await asyncio.sleep(retry_delay_seconds)

    return retried


def schedule_index_memory(memory_id: int, content: str,
                           memory_type: str, source: str,
                           priority: int = 0):
    """Enqueue a retrieval index job for a memory.

    Replaces the old fire-and-forget asyncio.create_task(index_memory(...)).
    The old pattern was unreliable on Vercel serverless: background tasks are
    killed when the function returns a response, so newly created memories were
    never indexed and became invisible to associative_retrieve().

    This implementation inserts a pending job row synchronously (~5 ms).
    The sentinel piggyback (process_pending_index_jobs) processes these jobs in
    batches every ~5 minutes with atomic status claiming and retry tracking.
    If a job already exists for this memory (pending/processing), this is a
    no-op — avoids duplicate queue entries.
    """
    if not config.indexing_enabled:
        return
    try:
        existing = supabase.table("pending_retrieval_index_jobs") \
            .select("id") \
            .eq("memory_id", memory_id) \
            .in_("status", ["pending", "processing"]) \
            .limit(1) \
            .execute()
        if existing and existing.data:
            return  # Already queued

        supabase.table("pending_retrieval_index_jobs").insert({
            "memory_id": memory_id,
            "content": content,
            "memory_type": memory_type or "note",
            "source": source,
            "priority": priority,
            "status": "pending",
        }).execute()
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"schedule_index_memory: failed to enqueue job for memory {memory_id}: {e}")


async def process_pending_index_jobs(max_jobs: int = 2) -> int:
    """Process pending retrieval index jobs.  Called by the sentinel piggyback.

    Processes up to max_jobs per call to stay within the sentinel's 30 s timeout
    (each index_memory call takes ~10-15 s due to LLM extraction).

    Flow:
      1. SELECT pending jobs ordered by priority DESC, created_at ASC.
      2. Atomically claim each job (UPDATE WHERE status='pending').
      3. Call index_memory() for the claimed job.
      4. Mark completed / failed.  Failed jobs are retried up to 3 times before
         being escalated to dead_letter.

    Returns the number of jobs processed.
    """
    if not config.indexing_enabled:
        return 0

    try:
        rows = supabase.table("pending_retrieval_index_jobs") \
            .select("id, memory_id, content, memory_type, source, retry_count") \
            .eq("status", "pending") \
            .order("priority", desc=True) \
            .order("created_at", desc=False) \
            .limit(max_jobs) \
            .execute()
    except Exception as e:
        audit_log_sync("retrieval", "WARNING", f"process_pending_index_jobs: fetch failed: {e}")
        return 0

    if not rows or not rows.data:
        return 0

    processed = 0
    for job in rows.data:
        job_id = job["id"]
        memory_id = job["memory_id"]

        # Atomic claim — only one sentinel run wins the race
        try:
            claim = supabase.table("pending_retrieval_index_jobs") \
                .update({
                    "status": "processing",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }) \
                .eq("id", job_id) \
                .eq("status", "pending") \
                .execute()
            if not claim.data:
                continue  # Another sentinel run already claimed it
        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"process_pending_index_jobs: claim failed for job {job_id}: {e}")
            continue

        try:
            success = await index_memory(
                memory_id=memory_id,
                content=job["content"],
                memory_type=job.get("memory_type", "note"),
                source=job.get("source", "sentinel-sweep"),
            )
        except Exception as e:
            success = False
            audit_log_sync("retrieval", "ERROR",
                           f"process_pending_index_jobs: index_memory failed for memory {memory_id}: {e}")

        retry_count = (job.get("retry_count") or 0) + 1
        if success:
            try:
                supabase.table("pending_retrieval_index_jobs") \
                    .update({
                        "status": "completed",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }) \
                    .eq("id", job_id) \
                    .execute()
            except Exception:
                pass
            audit_log_sync("retrieval", "INFO",
                           f"process_pending_index_jobs: indexed memory {memory_id} (job {job_id})")
        else:
            new_status = "dead_letter" if retry_count >= 3 else "pending"
            try:
                supabase.table("pending_retrieval_index_jobs") \
                    .update({
                        "status": new_status,
                        "retry_count": retry_count,
                        "error": f"index_memory returned False (attempt {retry_count})",
                    }) \
                    .eq("id", job_id) \
                    .execute()
            except Exception:
                pass
            audit_log_sync(
                "retrieval",
                "WARNING" if new_status == "pending" else "ERROR",
                f"process_pending_index_jobs: memory {memory_id} attempt {retry_count} "
                f"→ {new_status}",
            )

        processed += 1

    if processed:
        await update_node_stats()

    return processed
