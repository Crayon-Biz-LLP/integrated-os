#!/usr/bin/env python3
"""Backfill enrichment for retrieval passages.

Processes all memories without a completed index run at INDEX_VERSION=1.
Uses all 3 Gemini API keys via the existing MultiKeyLimiter infrastructure
(39 RPM for flash-lite extraction, 4200 RPM for embeddings).

Usage:
    # Count remaining (dry run)
    python3 scripts/backfill_enrichment.py --dry-run

    # Run backfill with default concurrency (3)
    python3 scripts/backfill_enrichment.py

    # Run with higher concurrency
    python3 scripts/backfill_enrichment.py --concurrency 6

    # Run with a limit
    python3 scripts/backfill_enrichment.py --limit 50
"""

import asyncio
import argparse
import time
import sys
import os
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase  # noqa: E402
from core.retrieval.pipeline import index_memory, INDEX_VERSION  # noqa: E402
from core.retrieval.graph import update_node_stats  # noqa: E402
from core.lib.audit_logger import audit_log_sync  # noqa: E402


def get_unindexed_memories(supabase) -> list:
    """Fetch all memories that don't have a completed index run at INDEX_VERSION=1."""
    # Get completed source_ids
    completed = supabase.table("retrieval_index_runs") \
        .select("source_id") \
        .eq("status", "completed") \
        .eq("index_version", INDEX_VERSION) \
        .execute()
    indexed_ids = set(int(r["source_id"]) for r in (completed.data or []))

    # Also include dead_letter — those are exhausted and won't benefit from retry
    dead_letter = supabase.table("retrieval_index_runs") \
        .select("source_id") \
        .eq("status", "dead_letter") \
        .eq("index_version", INDEX_VERSION) \
        .execute()
    dead_ids = set(int(r["source_id"]) for r in (dead_letter.data or []))

    skip_ids = indexed_ids | dead_ids

    # Fetch all memories
    all_mems = supabase.table("memories") \
        .select("id, content, memory_type") \
        .order("id", desc=False) \
        .execute()
    memories = all_mems.data or []

    # Filter to unindexed
    unindexed = [
        m for m in memories
        if m["id"] not in skip_ids and m.get("content")
    ]
    return unindexed, len(indexed_ids), len(dead_ids), len(memories)


async def process_memory(sem: asyncio.Semaphore, mem: dict, stats: dict,
                         supabase) -> bool:
    """Index a single memory with semaphore-controlled concurrency."""
    async with sem:
        mid = mem["id"]
        content = mem["content"]
        mem_type = mem.get("memory_type", "note")

        try:
            ok = await index_memory(
                memory_id=mid,
                content=content,
                memory_type=mem_type,
                source="backfill-enrichment",
            )
            if ok:
                stats["success"] += 1
            else:
                stats["failed"] += 1
            return ok
        except Exception as e:
            stats["errors"] += 1
            audit_log_sync("retrieval", "WARNING",
                           f"Backfill error for memory {mid}: {e}")
            return False


async def run_backfill(concurrency: int = 3, limit: int = 0, dry_run: bool = False):
    """Main backfill loop."""
    supabase = get_supabase()

    print("Fetching unindexed memories...")
    unindexed, indexed, dead, total = get_unindexed_memories(supabase)

    print(f"\n{'='*60}")
    print("  RETRIEVAL ENRICHMENT BACKFILL")
    print(f"{'='*60}")
    print(f"  Total memories:        {total}")
    print(f"  Already indexed:       {indexed}")
    print(f"  Dead letter:           {dead}")
    print(f"  Remaining:             {len(unindexed)}")
    print(f"  Concurrency:           {concurrency}")
    print("  Rate limit:            39 RPM (3 keys × 13 RPM)")
    if limit:
        unindexed = unindexed[:limit]
        print(f"  Limit:                 {limit}")
    print(f"{'='*60}\n")

    if dry_run:
        print("Dry run — not processing. Remove --dry-run to start.")
        return

    if not unindexed:
        print("Nothing to index. All memories are already processed.")
        return

    # Estimate time: ~3 extraction calls per memory at 39 RPM ≈ 13 memories/min
    est_minutes = len(unindexed) / 13
    print(f"Estimated time: ~{est_minutes:.0f} minutes\n")

    sem = asyncio.Semaphore(concurrency)
    stats = {"success": 0, "failed": 0, "errors": 0}
    start_time = time.time()

    # Process in batches of concurrency to show progress
    batch_size = concurrency * 2
    for i in range(0, len(unindexed), batch_size):
        batch = unindexed[i:i + batch_size]
        tasks = [
            process_memory(sem, mem, stats, supabase)
            for mem in batch
        ]
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        done = stats["success"] + stats["failed"] + stats["errors"]
        rate = done / (elapsed / 60) if elapsed > 0 else 0
        remaining = len(unindexed) - done
        eta_min = remaining / rate if rate > 0 else 0

        status_line = (f"  [{done}/{len(unindexed)}] "
              f"ok={stats['success']} fail={stats['failed']} err={stats['errors']} "
              f"| {rate:.1f}/min | ETA: {eta_min:.0f}min")
        print(status_line)
        # Write to status file for reliable monitoring (bypasses stdout buffering)
        try:
            with open("/tmp/backfill_status.txt", "w") as f:
                f.write(f"{datetime.now().isoformat()} {status_line}\n")
        except Exception:
            pass

    print("  Updating node stats...")
    await update_node_stats()

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print("  BACKFILL COMPLETE")
    print(f"{'='*60}")
    print(f"  Processed:  {stats['success'] + stats['failed'] + stats['errors']}")
    print(f"  Success:    {stats['success']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Errors:     {stats['errors']}")
    print(f"  Duration:   {elapsed/60:.1f} minutes")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill retrieval enrichment for unindexed memories"
    )
    parser.add_argument("--concurrency", type=int, default=3,
                        help="Max concurrent index_memory calls (default: 3)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max memories to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats without processing")
    args = parser.parse_args()

    asyncio.run(run_backfill(
        concurrency=args.concurrency,
        limit=args.limit,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
