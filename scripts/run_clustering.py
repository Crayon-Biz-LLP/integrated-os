#!/usr/bin/env python3
"""CLI entry point for weekly memory clustering (M5).

Usage:
    python scripts/run_clustering.py
"""
import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    from core.pulse.memory_clusters import build_memory_clusters
    result = await build_memory_clusters()
    print(f"Clusters created: {result['clusters_created']}")
    print(f"Clusters reused: {result['clusters_reused']}")
    print(f"Orphans: {result['orphans_count']}")
    print(f"Seeds processed: {result['seeds_processed']}")
    print(f"Quality histogram: {result['quality_histogram']}")

if __name__ == "__main__":
    asyncio.run(main())
