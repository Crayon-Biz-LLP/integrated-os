#!/usr/bin/env python3
"""Run memory clustering (M5a-M5c). Standalone entrypoint for GitHub Actions."""
import asyncio
import sys
import os

# Add repo root to sys.path so core modules are importable
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    import json
    from core.pulse.memory_clusters import build_memory_clusters
    result = await build_memory_clusters()
    print(json.dumps(result, indent=2, default=str))
    if result.get("errors"):
        # RuntimeError (not SystemExit) so arun_tenant_fanout's except
        # Exception isolates this tenant instead of aborting the loop.
        raise RuntimeError(f"Clustering failed: {result['errors']}")


if __name__ == "__main__":
    from core.services.db import arun_tenant_fanout

    asyncio.run(arun_tenant_fanout(main, job_name="memory_clusters"))
