import asyncio
from typing import Optional
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.retrieval.config import config, BACKFILL_BATCH_SIZE
from core.retrieval.pipeline import index_memory

supabase = get_supabase()

CHECKPOINT_KEY = "_backfill_checkpoint"
MAX_PARTIAL_RETRIES = 2


async def _load_checkpoint() -> Optional[int]:
    """Load the last fully-indexed memory ID from the persistent checkpoint."""
    res = supabase.table("retrieval_index_runs") \
        .select("source_id") \
        .eq("source_type", CHECKPOINT_KEY) \
        .eq("status", "completed") \
        .order("id", desc=True) \
        .limit(1) \
        .maybe_single() \
        .execute()
    if res and res.data:
        try:
            return int(res.data["source_id"])
        except (ValueError, TypeError):
            return None
    return None


async def _save_checkpoint(memory_id: int):
    """Persist the last fully-indexed memory ID."""
    try:
        supabase.table("retrieval_index_runs") \
            .upsert({
                "source_type": CHECKPOINT_KEY,
                "source_id": str(memory_id),
                "index_version": 0,
                "status": "completed",
                "started_at": None,
                "completed_at": None,
            }, on_conflict="source_type,source_id,index_version") \
            .execute()
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Failed to save checkpoint at memory {memory_id}: {e}")


async def _get_indexed_ids() -> set:
    """Get set of memory IDs that have a terminal index run."""
    res = supabase.table("retrieval_index_runs") \
        .select("source_id") \
        .eq("source_type", "memory") \
        .in_("status", ("completed", "dead_letter")) \
        .execute()
    ids = set()
    for r in (res.data or []):
        try:
            ids.add(int(r["source_id"]))
        except (ValueError, TypeError):
            pass
    return ids


async def backfill_memories(
    batch_size: int = BACKFILL_BATCH_SIZE,
    resume_from_id: Optional[int] = None,
    dry_run: bool = False,
    auto_resume: bool = True,
) -> dict:
    """One-time backfill: index all eligible memories into the retrieval substrate.

    Resumable: auto-computes resume point from last successful index run (forward pass).
    Conservative: after forward pass, sweeps completed_partial and failed runs for retry.
    Checkpointed: persists last fully-indexed batch boundary for crash recovery.

    Returns summary dict.
    """
    if not config.indexing_enabled and not dry_run:
        return {"status": "skipped", "reason": "indexing_disabled"}

    if auto_resume and resume_from_id is None:
        resume_from_id = await _load_checkpoint()
        if resume_from_id:
            audit_log_sync("retrieval", "INFO",
                           f"Checkpoint resume from memory ID {resume_from_id}")
            print(f"[BACKFILL] Resuming from memory ID {resume_from_id}", flush=True)

    print("[BACKFILL] Starting...", flush=True)
    total = 0
    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0

    done_ids = await _get_indexed_ids()

    async def _index_row(row: dict) -> bool:
        nonlocal processed, succeeded, failed, skipped
        processed += 1
        try:
            ok = await index_memory(
                memory_id=row["id"],
                content=row["content"] or "",
                memory_type=row.get("memory_type") or "memory",
                source=row.get("source") or "unknown",
                metadata=row.get("metadata"),
            )
            if ok:
                succeeded += 1
                return True
            else:
                skipped += 1
                return False
        except Exception as e:
            failed += 1
            audit_log_sync("retrieval", "WARNING",
                           f"Backfill failed for memory {row['id']}: {e}")
            return False

    # --- Forward pass: process all memories in ascending ID order ---
    checkpoint = resume_from_id

    while True:
        query = supabase.table("memories") \
            .select("id, content, memory_type, source, metadata, created_at") \
            .eq("is_current", True) \
            .eq("pruned", False) \
            .not_.is_("embedding", "null") \
            .order("id") \
            .limit(batch_size)

        if checkpoint:
            query = query.gt("id", checkpoint)

        res = query.execute()
        batch = res.data if res and res.data else []

        if not batch:
            break

        # Filter out already-indexed memories
        batch = [r for r in batch if r["id"] not in done_ids]
        if not batch:
            checkpoint = checkpoint or 0
            checkpoint = max(r["id"] for r in (res.data or [])) if res and res.data else checkpoint
            # If batch is fully filtered, advance checkpoint beyond this batch
            # and continue to next batch
            if len(batch) == 0 and res.data:
                last_in_raw = res.data[-1]["id"]
                checkpoint = last_in_raw
                await _save_checkpoint(last_in_raw)
                if len(res.data) < batch_size:
                    break
                continue

        total += len(batch)

        if dry_run:
            skipped += len(batch)
            continue

        tasks = [_index_row(row) for row in batch]
        await asyncio.gather(*tasks)

        # Save checkpoint at end of batch
        last_id = batch[-1]["id"]
        checkpoint = last_id
        await _save_checkpoint(last_id)

        print(f"[BACKFILL] Batch: {processed} processed, {succeeded} OK, "
              f"{failed} fail, {skipped} skip | Last ID: {last_id} | "
              f"~{total}/{total} from batch | continues...", flush=True)

        if len(res.data) < batch_size:
            break

    # --- Partials sweep: pick up any completed_partial or failed runs ---
    partials = supabase.table("retrieval_index_runs") \
        .select("source_type, source_id, retry_count") \
        .eq("source_type", "memory") \
        .in_("status", ("completed_partial", "failed")) \
        .lt("retry_count", MAX_PARTIAL_RETRIES) \
        .limit(batch_size) \
        .execute()

    for p in (partials.data or []):
        mem = supabase.table("memories") \
            .select("id, content, memory_type, source, metadata") \
            .eq("id", int(p["source_id"])) \
            .maybe_single() \
            .execute()
        if not mem or not mem.data:
            audit_log_sync("retrieval", "WARNING",
                           f"Partials sweep: memory {p['source_id']} not found")
            continue

        audit_log_sync("retrieval", "INFO",
                       f"Partials sweep: retrying memory {p['source_id']} "
                       f"(status was {p['status']}, retry {p['retry_count']})")
        await _index_row(mem.data)

    from core.retrieval.graph import update_node_stats
    await update_node_stats()

    print(f"[BACKFILL] DONE: {succeeded}/{processed} succeeded, "
          f"{failed} failed, {skipped} skipped, "
          f"{len(partials.data or [])} partials swept", flush=True)

    return {
        "status": "completed" if not dry_run else "dry_run",
        "total": total,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "resume_from_id": checkpoint,
        "partials_swept": len(partials.data or []),
        "dry_run": dry_run,
    }


async def backfill_single_memory(memory_id: int) -> bool:
    """Index a single memory by ID. Useful for testing or ad-hoc reindexing."""
    try:
        res = supabase.table("memories") \
            .select("id, content, memory_type, source, metadata") \
            .eq("id", memory_id) \
            .maybe_single() \
            .execute()

        if not res or not res.data:
            audit_log_sync("retrieval", "WARNING", f"Memory {memory_id} not found")
            return False

        row = res.data
        return await index_memory(
            memory_id=row["id"],
            content=row["content"] or "",
            memory_type=row.get("memory_type") or "memory",
            source=row.get("source") or "unknown",
            metadata=row.get("metadata"),
        )

    except Exception as e:
        audit_log_sync("retrieval", "WARNING", f"Single backfill failed for {memory_id}: {e}")
        return False
