"""Backfill orphan tasks with org linkage from their notes field.

One-time migration script. Uses extract_context_from_source (deterministic + LLM)
to find orgs in the task's notes/title and link them.

Usage:
    python scripts/backfill_orphan_tasks.py [--dry-run] [--limit N]
"""

import asyncio
import argparse
import os
from supabase import create_client
from core.services.db import tenant_scope


async def backfill_orphan_tasks(dry_run: bool = False, limit: int = 100, owner_id: str = None):
    """Backfill orphan tasks with org linkage."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    supabase = create_client(url, key)

    # Find all tenants to process
    if owner_id:
        tenant_ids = [owner_id]
    else:
        users = supabase.table('users').select('id').execute()
        tenant_ids = [u['id'] for u in (users.data or [])]

    total_backfilled = 0
    total_failed = 0
    total_no_org = 0

    for tid in tenant_ids:
        print(f"\n{'='*60}")
        print(f"Tenant: {tid[:8]}...")
        with tenant_scope(tid):
            from core.services.db import tenant_aware_client
            supabase_tenant = tenant_aware_client()

            query = supabase_tenant.table('tasks').select('id, title, notes').eq(
                'is_current', True
            ).is_('organization_id', None).is_('pending_org_id', None)
            orphans = query.limit(limit).execute()

            if not orphans.data:
                print("  No orphan tasks found.")
                continue

            print(f"  Found {len(orphans.data)} orphan tasks. Processing...")

            for task in orphans.data:
                task_id = task['id']
                text = task.get('notes') or task.get('title') or ""
                if not text:
                    total_no_org += 1
                    continue

                try:
                    from core.lib.entity_context import extract_context_from_source
                    ctx = await extract_context_from_source(text)

                    if ctx.organization_id:
                        if not dry_run:
                            supabase_tenant.table('tasks').update({
                                'organization_id': ctx.organization_id,
                            }).eq('id', task_id).execute()
                        print(f"  ✅ Task {task_id}: linked to org {ctx.organization_name} ({ctx.organization_id[:8]}...)")
                        total_backfilled += 1
                    elif ctx.pending_org_id:
                        if not dry_run:
                            supabase_tenant.table('tasks').update({
                                'pending_org_id': ctx.pending_org_id,
                            }).eq('id', task_id).execute()
                        print(f"  📋 Task {task_id}: linked to pending org {ctx.pending_org_label} (pending#{ctx.pending_org_id})")
                        total_backfilled += 1
                    else:
                        total_no_org += 1
                        print(f"  ❌ Task {task_id}: no org found in '{text[:50]}...'")

                except Exception as e:
                    total_failed += 1
                    print(f"  ⚠️ Task {task_id}: error: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {total_backfilled} backfilled, {total_no_org} no org found, {total_failed} failed")
    if dry_run:
        print("(DRY RUN — no changes made)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill orphan tasks with org linkage")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--limit", type=int, default=100, help="Max tasks to process")
    parser.add_argument("--owner-id", type=str, help="Filter by owner (tenant) UUID")
    args = parser.parse_args()

    asyncio.run(backfill_orphan_tasks(dry_run=args.dry_run, limit=args.limit, owner_id=args.owner_id))
