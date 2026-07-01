#!/usr/bin/env python3
"""CLI entry point for system maintenance tasks.

Usage:
    python scripts/run_maintenance.py              # standard mode
    python scripts/run_maintenance.py daily        # daily mode
    python scripts/run_maintenance.py weekly       # weekly mode

Mode can also be set via MAINTENANCE_MODE env var.
"""
import asyncio
import sys
import os

# Add repo root to sys.path so core modules are importable
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else os.getenv("MAINTENANCE_MODE", "standard")
    if mode not in ("standard", "daily", "weekly"):
        print(f"Invalid mode: {mode}. Must be standard, daily, or weekly.")
        sys.exit(1)

    from core.pulse.maintenance import process_maintenance
    result = await process_maintenance(mode=mode)
    for k, v in result.items():
        print(f"{k}: {v}")

if __name__ == "__main__":
    asyncio.run(main())
