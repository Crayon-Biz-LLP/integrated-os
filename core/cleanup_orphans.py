#!/usr/bin/env python3
"""
Cleanup Orphans - Remove orphaned records from Supabase tables.
Orphans are records that reference non-existent parent records.

Usage:
    python core/cleanup_orphans.py [--dry-run]
"""
import os
import sys
import asyncio
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_key:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    sys.exit(1)

supabase = create_client(supabase_url, supabase_key)


def cleanup_orphan_graph_edges():
    """Remove graph_edges pointing to non-existent nodes."""
    print("🔍 Checking for orphaned graph_edges...")
    
    # With CASCADE foreign keys, this should now be rare
    # But keeping as safety net
    nodes = supabase.table("graph_nodes").select("id").execute()
    valid_node_ids = {n["id"] for n in (nodes.data or [])}
    
    edges = supabase.table("graph_edges").select("id, source_node_id, target_node_id").execute()
    
    orphan_edges = []
    for edge in (edges.data or []):
        if edge["source_node_id"] not in valid_node_ids or edge["target_node_id"] not in valid_node_ids:
            orphan_edges.append(edge["id"])
    
    if orphan_edges:
        print(f"  Found {len(orphan_edges)} orphaned edges (THIS SHOULD BE RARE WITH CASCADE)")
        if "--dry-run" not in sys.argv:
            # Log before delete for investigation
            for eid in orphan_edges:
                audit_log_sync("cleanup", "WARNING", f"Orphaned edge found: {eid}")
            supabase.table("graph_edges").delete().in_("id", orphan_edges).execute()
            print(f"  ✅ Deleted {len(orphan_edges)} orphaned edges")
    else:
        print("  ✅ No orphaned edges found")


def cleanup_orphan_tasks():
    """Remove tasks pointing to non-existent projects."""
    print("🔍 Checking for orphaned tasks...")
    
    # Fetch all valid project IDs
    projects = supabase.table("projects").select("id").execute()
    valid_project_ids = {p["id"] for p in (projects.data or [])}
    
    # Fetch all tasks
    tasks = supabase.table("tasks").select("id, project_id").execute()
    
    orphan_tasks = []
    for task in (tasks.data or []):
        if task.get("project_id") and task["project_id"] not in valid_project_ids:
            orphan_tasks.append(task["id"])
    
    if orphan_tasks:
        print(f"  Found {len(orphan_tasks)} orphaned tasks")
        if "--dry-run" not in sys.argv:
            supabase.table("tasks").delete().in_("id", orphan_tasks).execute()
            print(f"  ✅ Deleted {len(orphan_tasks)} orphaned tasks")
    else:
        print("  ✅ No orphaned tasks found")


def cleanup_orphan_memories():
    """Remove memories pointing to non-existent projects."""
    print("🔍 Checking for orphaned memories...")
    
    # Fetch all valid project IDs
    projects = supabase.table("projects").select("id").execute()
    valid_project_ids = {p["id"] for p in (projects.data or [])}
    
    # Fetch all memories
    memories = supabase.table("memories").select("id, project_id").execute()
    
    orphan_memories = []
    for mem in (memories.data or []):
        if mem.get("project_id") and mem["project_id"] not in valid_project_ids:
            orphan_memories.append(mem["id"])
    
    if orphan_memories:
        print(f"  Found {len(orphan_memories)} orphaned memories")
        if "--dry-run" not in sys.argv:
            # Soft delete: mark as pruned
            for mem_id in orphan_memories:
                supabase.table("memories").update({
                    "metadata": '{"pruned": true, "pruned_reason": "orphaned"}'
                }).eq("id", mem_id).execute()
            print(f"  ✅ Marked {len(orphan_memories)} orphaned memories as pruned")
    else:
        print("  ✅ No orphaned memories found")


def cleanup_orphan_raw_dumps():
    """Remove raw_dumps older than 90 days (aligned with memories pruning window)."""
    print("🔍 Checking for orphaned raw_dumps...")
    
    # Delete old raw_dumps (90 days to align with memories pruning)
    ninety_days_ago = (datetime.now() - timedelta(days=90)).isoformat()
    
    old_dumps = supabase.table("raw_dumps") \
        .select("id") \
        .eq("status", "completed") \
        .lt("created_at", ninety_days_ago) \
        .execute()
    
    if old_dumps.data:
        dump_ids = [d["id"] for d in old_dumps.data]
        print(f"  Found {len(dump_ids)} old completed raw_dumps")
        if "--dry-run" not in sys.argv:
            supabase.table("raw_dumps").delete().in_("id", dump_ids).execute()
            print(f"  ✅ Deleted {len(dump_ids)} old raw_dumps")
    else:
        print("  ✅ No old raw_dumps found")


if __name__ == "__main__":
    print("🧹 Starting orphan cleanup...")
    print(f"   Mode: {'DRY RUN' if '--dry-run' in sys.argv else 'LIVE'}")
    print()
    
    cleanup_orphan_graph_edges()
    cleanup_orphan_tasks()
    cleanup_orphan_memories()
    cleanup_orphan_raw_dumps()
    
    print()
    print("✅ Cleanup complete!")
