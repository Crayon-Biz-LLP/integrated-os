#!/usr/bin/env python3
"""
Migrate CHURCH entity tag to ASHRAYA across all database tables.

Usage:
    python core/skills/migrate_church_to_ashraya.py          # Perform migration
    python core/skills/migrate_church_to_ashraya.py --dry-run # Preview only
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

OLD_TAG = "CHURCH"
NEW_TAG = "ASHRAYA"

MIGRATIONS = []


def count_records(table: str, column: str, value: str) -> int:
    q = supabase.table(table).select("id", count="exact").eq(column, value).execute()
    return q.count if hasattr(q, 'count') else len(q.data or [])


def count_jsonb(table: str, jsonb_path: str, value: str) -> int:
    data = supabase.table(table).select("id").eq(jsonb_path, value).execute()
    return len(data.data or [])


def update_column(table: str, column: str, old: str, new: str) -> int:
    data = supabase.table(table).select("id").eq(column, old).execute()
    ids = [r["id"] for r in (data.data or [])]
    if not ids:
        return 0
    supabase.table(table).update({column: new}).eq(column, old).execute()
    return len(ids)


def update_jsonb(table: str, jsonb_path: str, old: str, new: str) -> int:
    data = supabase.table(table).select("id, metadata").eq(jsonb_path, old).execute()
    ids = []
    for r in (data.data or []):
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except:
                continue
        if not isinstance(meta, dict):
            continue

        key = jsonb_path.split("->>")[-1]
        if meta.get(key) == old:
            meta[key] = new
            supabase.table(table).update({"metadata": meta}).eq("id", r["id"]).execute()
            ids.append(r["id"])
    return len(ids)


def migrate_all(dry_run: bool = False):
    print(f"\n{'='*60}")
    print(f"  CHURCH → ASHRAYA Migration")
    print(f"  Mode: {'DRY RUN (no writes)' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    results = []

    # 1. projects.org_tag
    count = count_records("projects", "org_tag", OLD_TAG)
    results.append(("projects", "org_tag", count))
    if not dry_run and count > 0:
        updated = update_column("projects", "org_tag", OLD_TAG, NEW_TAG)
        print(f"  ✓ projects.org_tag: {updated} rows updated")
    else:
        print(f"  ~ projects.org_tag: {count} rows {'would be updated' if dry_run else 'up to date'}")

    # 1b. projects.name (where name = 'Church')
    count_name = count_records("projects", "name", "Church")
    results.append(("projects", "name", count_name))
    if not dry_run and count_name > 0:
        updated = update_column("projects", "name", "Church", "Ashraya")
        print(f"  ✓ projects.name: {updated} rows updated")
    else:
        print(f"  ~ projects.name: {count_name} rows {'would be updated' if dry_run else 'up to date'}")

    # 2. tasks.org_tag
    count = count_records("tasks", "org_tag", OLD_TAG)
    results.append(("tasks", "org_tag", count))
    if not dry_run and count > 0:
        updated = update_column("tasks", "org_tag", OLD_TAG, NEW_TAG)
        print(f"  ✓ tasks.org_tag: {updated} rows updated")
    else:
        print(f"  ~ tasks.org_tag: {count} rows {'would be updated' if dry_run else 'up to date'}")

    # 3. graph_nodes metadata->>org_tag (project nodes)
    count = count_jsonb("graph_nodes", "metadata->>org_tag", OLD_TAG)
    results.append(("graph_nodes", "metadata->>org_tag", count))
    if not dry_run and count > 0:
        updated = update_jsonb("graph_nodes", "metadata->>org_tag", OLD_TAG, NEW_TAG)
        print(f"  ✓ graph_nodes metadata->>org_tag: {updated} rows updated")
    else:
        print(f"  ~ graph_nodes metadata->>org_tag: {count} rows {'would be updated' if dry_run else 'up to date'}")

    # 4. graph_nodes metadata->>entity (practice/declared nodes)
    count = count_jsonb("graph_nodes", "metadata->>entity", OLD_TAG)
    results.append(("graph_nodes", "metadata->>entity", count))
    if not dry_run and count > 0:
        updated = update_jsonb("graph_nodes", "metadata->>entity", OLD_TAG, NEW_TAG)
        print(f"  ✓ graph_nodes metadata->>entity: {updated} rows updated")
    else:
        print(f"  ~ graph_nodes metadata->>entity: {count} rows {'would be updated' if dry_run else 'up to date'}")

    # 5. raw_dumps metadata->>entity (archival)
    count = count_jsonb("raw_dumps", "metadata->>entity", OLD_TAG)
    results.append(("raw_dumps", "metadata->>entity", count))
    if not dry_run and count > 0:
        updated = update_jsonb("raw_dumps", "metadata->>entity", OLD_TAG, NEW_TAG)
        print(f"  ✓ raw_dumps metadata->>entity: {updated} rows updated")
    else:
        print(f"  ~ raw_dumps metadata->>entity: {count} rows {'would be updated' if dry_run else 'up to date'}")

    # 6. resources.category
    count = count_records("resources", "category", OLD_TAG)
    results.append(("resources", "category", count))
    if not dry_run and count > 0:
        updated = update_column("resources", "category", OLD_TAG, NEW_TAG)
        print(f"  ✓ resources.category: {updated} rows updated")
    else:
        print(f"  ~ resources.category: {count} rows {'would be updated' if dry_run else 'up to date'}")

    print(f"\n{'='*60}")
    total = sum(r[2] for r in results)
    if dry_run:
        print(f"  DRY RUN COMPLETE — {total} total records would be affected.")
        print(f"  Run without --dry-run to apply.")
    else:
        print(f"  MIGRATION COMPLETE — {total} total records updated.")
    print(f"{'='*60}\n")

    for table, column, count in results:
        status = "✓" if count > 0 else "—"
        print(f"  {status} {table}.{column}: {count} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate CHURCH tag to ASHRAYA")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()
    migrate_all(dry_run=args.dry_run)
