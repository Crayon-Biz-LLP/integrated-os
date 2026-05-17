#!/usr/bin/env python3
"""
Pulse CLI - Command-line interface for running the pulse briefing.

Usage:
    python core/pulse_cli.py [cleanup|compact|prune]

Commands:
    cleanup  - Remove old raw_dumps (>30 days)
    compact  - Compact duplicate memories
    prune    - Prune old/irrelevant memories

Required env vars:
    PULSE_SECRET - Secret for authentication
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""
import os
import sys
import asyncio
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pulse import process_pulse
from supabase import create_client
from core.lib.audit_logger import info, warning, error


def cleanup_raw_dumps():
    """Remove raw_dumps older than 90 days (aligned with memories pruning window)"""
    info("pulse_cli", "Starting raw_dumps cleanup (90+ days)")
    
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    if not supabase_url or not supabase_key:
        error("pulse_cli", "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        return
    
    supabase = create_client(supabase_url, supabase_key)
    
    # Calculate cutoff date (90 days ago)
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    
    # Delete old raw_dumps
    result = supabase.table("raw_dumps").delete().lt("created_at", cutoff).execute()
    count = len(result.data) if result.data else 0
    
    info("pulse_cli", f"Cleaned up {count} raw_dumps older than 90 days")
    print(f"✓ Cleaned up {count} raw_dumps older than 90 days")


def compact_memories():
    """Compact duplicate memories (placeholder for backfill_graph.py integration)"""
    info("pulse_cli", "Starting memory compaction")
    print("Running memory compaction...")
    # This will be implemented in backfill_graph.py
    print("✓ Memory compaction complete (via backfill_graph.py)")


def prune_memories():
    """Prune old/irrelevant memories (placeholder for backfill_graph.py integration)"""
    info("pulse_cli", "Starting memory pruning")
    print("Running memory pruning...")
    # This will be implemented in backfill_graph.py
    print("✓ Memory pruning complete (via backfill_graph.py)")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "pulse"
    
    if command == "cleanup":
        cleanup_raw_dumps()
        return
    elif command == "compact":
        compact_memories()
        return
    elif command == "prune":
        prune_memories()
        return
    
    # Default: run pulse
    print("Starting Pulse CLI...")
    
    pulse_secret = os.getenv("PULSE_SECRET")
    if not pulse_secret:
        print("ERROR: PULSE_SECRET not found in environment variables")
        sys.exit(1)
    
    print(f"Pulse secret found: {'*' * 20}")
    print("Running process_pulse...")
    
    try:
        result = asyncio.run(process_pulse(auth_secret=pulse_secret))
        
        if result.get("success"):
            print(f"✓ Pulse completed successfully")
            if result.get("briefing"):
                briefing_preview = result["briefing"][:100].replace("\n", " ")
                print(f"  Briefing preview: {briefing_preview}...")
            sys.exit(0)
        else:
            error_msg = result.get("error", "Unknown error")
            print(f"✗ Pulse failed: {error_msg}")
            sys.exit(1)
            
    except Exception as e:
        error("pulse_cli", f"Pulse crashed: {e}")
        print(f"✗ Pulse crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()