#!/usr/bin/env python3
"""CLI entry point for retrieval index backfill.

Usage:
    python scripts/run_backfill.py                  # batch_size=5, auto_resume=True
    python scripts/run_backfill.py 10               # batch_size=10
    python scripts/run_backfill.py 20 --no-resume   # batch_size=20, fresh start
"""
import asyncio
import sys
import os

# Add repo root to sys.path so core modules are importable
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("BACKFILL_BATCH_SIZE", "5"))
    auto_resume = "--no-resume" not in sys.argv

    from core.retrieval.backfill import backfill_memories
    result = await backfill_memories(batch_size=batch_size, auto_resume=auto_resume)
    print(f"Status: {result['status']}")
    print(f"Processed: {result['processed']}")
    print(f"Succeeded: {result['succeeded']}")
    print(f"Failed: {result['failed']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Resume from ID: {result['resume_from_id']}")
    print(f"Partials swept: {result['partials_swept']}")

if __name__ == "__main__":
    asyncio.run(main())
