#!/usr/bin/env python3
"""Run memory clustering (M5a-M5c). Standalone entrypoint for GitHub Actions."""
import asyncio
import json
from core.pulse.memory_clusters import build_memory_clusters


async def main():
    result = await build_memory_clusters()
    print(json.dumps(result, indent=2, default=str))
    if result.get("errors"):
        raise SystemExit(f"Clustering failed: {result['errors']}")


if __name__ == "__main__":
    asyncio.run(main())
