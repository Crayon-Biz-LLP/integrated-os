"""
Backfill Conversation Embeddings (Phase 1 — Option H)

Computes and stores embeddings for all existing conversation exchanges
that don't have them yet. Uses the same get_embedding() function as the
main pipeline (gemini-embedding-2-preview, 768 dimensions).

Usage:
    LIVE_DB=true python scripts/backfill_conversation_embeddings.py

Idempotent — safe to re-run. Only processes rows with NULL embedding.
"""

import os
import sys
import asyncio
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.services.db import get_supabase
from core.llm.embedding import get_embedding

BATCH_SIZE = 10
SLEEP_BETWEEN_BATCHES = 1.0
SEMAPHORE_LIMIT = 3


async def backfill_embeddings():
    supabase = get_supabase()
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    res = supabase.table('conversations') \
        .select('id, content, role') \
        .is_('embedding', 'null') \
        .eq('role', 'user') \
        .order('created_at', desc=False) \
        .execute()

    rows = res.data or []
    total = len(rows)
    print(f"Found {total} exchanges without embeddings.")

    if total == 0:
        print("Nothing to backfill.")
        return

    processed = 0
    failed = 0
    start_time = time.time()

    async def process_one(exchange_id, content):
        async with sem:
            try:
                result = await get_embedding(content)
                if result and result.success and result.vector:
                    await asyncio.to_thread(
                        lambda: supabase.table('conversations')
                            .update({'embedding': result.vector})
                            .eq('id', exchange_id)
                            .execute()
                    )
                    return True
                else:
                    reason = result.degraded_reason if result else 'unknown'
                    print(f"  WARNING: Embedding failed for exchange {exchange_id}: {reason}")
                    return False
            except Exception as e:
                print(f"  ERROR: Exception for exchange {exchange_id}: {e}")
                return False

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        tasks = [process_one(r['id'], r.get('content', '')) for r in batch if r.get('content', '').strip()]
        results = await asyncio.gather(*tasks)
        batch_ok = sum(1 for r in results if r)
        processed += batch_ok
        failed += (len(tasks) - batch_ok)

        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        eta = (total - processed) / rate if rate > 0 else 0
        print(f"  [{processed}/{total}] {batch_ok}/{len(tasks)} ok, "
              f"{rate:.1f} items/sec, ETA: {eta:.0f}s")
        await asyncio.sleep(SLEEP_BETWEEN_BATCHES)

    elapsed = time.time() - start_time
    print(f"\nBackfill complete: {processed} succeeded, {failed} failed, "
          f"{elapsed:.1f}s total ({processed/elapsed:.1f} items/sec)")


if __name__ == '__main__':
    asyncio.run(backfill_embeddings())
