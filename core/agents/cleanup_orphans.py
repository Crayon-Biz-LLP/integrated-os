#!/usr/bin/env python3
"""
Cleanup Orphans - Remove orphaned records from Supabase tables.
Orphans are records that reference non-existent parent records.

Usage:
    python core/cleanup_orphans.py [--dry-run]
"""
import sys
from datetime import datetime, timedelta, timezone

from core.lib.audit_logger import audit_log_sync
from core.services.db import get_supabase

supabase = get_supabase()


def cleanup_orphan_graph_edges(dry_run: bool = False):
    audit_log_sync("cleanup_orphans", "INFO", "Starting orphan graph edge cleanup...")
    all_edges = supabase.table("graph_edges").select("id, source_node_id, target_node_id").execute()
    orphans = 0
    for edge in all_edges.data or []:
        src = supabase.table("graph_nodes").select("id").eq("id", edge["source_node_id"]).execute()
        tgt = supabase.table("graph_nodes").select("id").eq("id", edge["target_node_id"]).execute()
        if not src.data or not tgt.data:
            orphans += 1
            if not dry_run:
                supabase.table("graph_edges").delete().eq("id", edge["id"]).execute()
                audit_log_sync("cleanup_orphans", "INFO", f"Deleted orphan edge {edge['id']}")
    if orphans:
        msg = f"Deleted {orphans} orphan graph edges."
        audit_log_sync("cleanup_orphans", "INFO", msg)
        print(f"  {msg}")
    else:
        print("  No orphan graph edges found.")


def cleanup_orphan_tasks(dry_run: bool = False):
    audit_log_sync("cleanup_orphans", "INFO", "Starting orphan task cleanup...")
    all_tasks = supabase.table("tasks").select("id, project_id, title").eq('is_current', True).execute()
    orphans = 0
    for task in all_tasks.data or []:
        pid = task.get("project_id")
        if not pid:
            continue
        proj = supabase.table("projects").select("id").eq("id", pid).execute()
        if not proj.data:
            orphans += 1
            if not dry_run:
                supabase.table("tasks").update({
                    "project_id": None,
                    "is_current": True
                }).eq("id", task["id"]).execute()
                audit_log_sync("cleanup_orphans", "INFO",
                              f"Unlinked task {task['id']} ('{task['title']}') from missing project {pid}")
    if orphans:
        msg = f"Unlinked {orphans} orphan tasks from missing projects."
        audit_log_sync("cleanup_orphans", "INFO", msg)
        print(f"  {msg}")
    else:
        print("  No orphan tasks found.")



def cleanup_orphan_raw_dumps(dry_run: bool = False):
    audit_log_sync("cleanup_orphans", "INFO", "Starting orphan raw_dumps cleanup...")
    before = supabase.table("raw_dumps").select("id", count="exact").neq("status", "completed").execute()
    count_before = before.count or 0
    if count_before == 0:
        print("  No raw_dumps to clean.")
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stale = supabase.table("raw_dumps") \
        .select("id", count="exact") \
        .neq("status", "completed") \
        .lt("created_at", cutoff) \
        .execute()
    stale_count = stale.count or 0
    if stale_count == 0:
        print("  No stale raw_dumps found.")
        return
    if not dry_run:
        supabase.table("raw_dumps") \
            .update({"status": "completed", "is_processed": True}) \
            .neq("status", "completed") \
            .lt("created_at", cutoff) \
            .execute()
    msg = f"Cleaned up {stale_count} stale raw_dumps."
    audit_log_sync("cleanup_orphans", "INFO", msg)
    print(f"  {msg}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    force = "--yes" in sys.argv or "--force" in sys.argv
    if dry_run:
        print("DRY RUN MODE — no changes will be made\n")
    elif not force:
        confirm = input("Are you sure you want to clean up orphan records? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    print("Starting orphan cleanup...\n")
    print("Graph Edges:")
    cleanup_orphan_graph_edges(dry_run)
    print("Tasks:")
    cleanup_orphan_tasks(dry_run)
    print("Raw Dumps:")
    cleanup_orphan_raw_dumps(dry_run)
    print("\nCleanup complete.")
