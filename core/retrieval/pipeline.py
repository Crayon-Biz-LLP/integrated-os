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
    build_triple_graph, upsert_memory_bundle_link, upsert_passage_triple_link,
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

        inserted_passages = []
        for p in passages:
            p_id = await _upsert_passage(p)
            if p_id:
                inserted_passages.append((p_id, p))

        for p_id, p in inserted_passages:
            if memory_id:
                await upsert_memory_bundle_link(memory_id, p_id)

        async def process_passage(p_id: int, p: Passage) -> Tuple[bool, bool]:
            """Returns (had_triples, llm_ok). llm_ok=False means LLM call failed."""
            async with index_semaphore:
                triples, llm_ok = await extract_triples(
                    text=p.text,
                    source_type=source_type,
                    source_id=source_id,
                    passage_id=p_id,
                    index_version=INDEX_VERSION,
                )
                if not llm_ok or not triples:
                    return bool(triples), llm_ok
                await build_triple_graph(triples, p_id, source_type, source_id)
                for triple in triples:
                    t_id = await _insert_triple(triple)
                    if t_id:
                        await upsert_passage_triple_link(p_id, t_id)
                return True, True

        tasks = [process_passage(p_id, p) for p_id, p in inserted_passages]
        results: List[Tuple[bool, bool]] = await asyncio.gather(*tasks)

        any_failure = any(not r[1] for r in results)
        any_success = any(r[1] for r in results)

        if not any_success:
            _set_run_status(run_id, "failed",
                            error="All passage extractions failed (LLM errors)")
            return False

        if any_failure:
            _set_run_status(run_id, "completed_partial",
                            error="Some passage extractions failed (LLM errors)")
            audit_log_sync("retrieval", "WARNING",
                           f"Index {run_id} for {source_type}/{source_id} completed partial: "
                           f"{sum(1 for r in results if not r[1])}/{len(results)} passages failed")
        else:
            _set_run_status(run_id, "completed")

        await update_node_stats()
        return True

    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"Index failed for {source_type}/{source_id}: {e}")
        _set_run_status(run_id, "failed", error=str(e)[:500])
        return False


async def _upsert_passage(passage: Passage) -> Optional[int]:
    """Upsert a passage, return its ID."""
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

        emb_res = await get_embedding(passage.text)
        if not emb_res or not emb_res.vector:
            audit_log_sync("retrieval", "WARNING",
                           f"Embedding returned None for passage {passage.passage_index} "
                           f"({passage.source_type}/{passage.source_id})")

        result = supabase.table("retrieval_passages") \
            .insert({
                "source_type": passage.source_type,
                "source_id": passage.source_id,
                "memory_id": passage.memory_id,
                "passage_index": passage.passage_index,
                "text": passage.text,
                "char_count": passage.char_count,
                "embedding": emb_res.vector if emb_res else None,
                "source_fingerprint": passage.source_fingerprint,
                "index_version": passage.index_version,
                "metadata": passage.metadata,
            }) \
            .execute()

        if result and result.data:
            return result.data[0]["id"]
        return None

    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"upsert_passage failed for {passage.source_type}/{passage.source_id} "
                       f"passage {passage.passage_index}: {e}")
        return None


async def _insert_triple(triple) -> Optional[int]:
    """Insert a triple, skip if duplicate. Return ID."""
    try:
        existing = supabase.table("retrieval_triples") \
            .select("id") \
            .eq("passage_id", triple.passage_id) \
            .eq("normalized_subject", triple.normalized_subject) \
            .eq("normalized_predicate", triple.normalized_predicate) \
            .eq("normalized_object", triple.normalized_object) \
            .eq("index_version", triple.index_version) \
            .maybe_single() \
            .execute()

        if existing and existing.data:
            return existing.data["id"]

        result = supabase.table("retrieval_triples") \
            .insert({
                "source_type": triple.source_type,
                "source_id": triple.source_id,
                "passage_id": triple.passage_id,
                "subject_text": triple.subject_text,
                "predicate_text": triple.predicate_text,
                "object_text": triple.object_text,
                "normalized_subject": triple.normalized_subject,
                "normalized_predicate": triple.normalized_predicate,
                "normalized_object": triple.normalized_object,
                "confidence": triple.confidence,
                "extraction_model": triple.extraction_model,
                "index_version": triple.index_version,
            }) \
            .execute()

        if result and result.data:
            return result.data[0]["id"]
        return None

    except Exception as e:
        audit_log_sync("retrieval", "ERROR",
                       f"insert_triple failed for passage {triple.passage_id}: {e}")
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
                           memory_type: str, source: str):
    """Fire-and-forget index_memory if retrieval indexing is enabled.
    Safe to call from async functions with a running event loop.
    Silent no-op if indexing is disabled or no event loop is running.
    """
    if not config.indexing_enabled:
        return
    try:
        asyncio.create_task(index_memory(
            memory_id=memory_id, content=content,
            memory_type=memory_type, source=source,
        ))
    except RuntimeError:
        pass  # No event loop in current thread
