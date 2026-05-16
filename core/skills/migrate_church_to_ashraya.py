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
from postgrest.exceptions import APIError

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


def safe_count(table: str, column: str, value: str) -> tuple:
    try:
        q = supabase.table(table).select("id", count="exact").eq(column, value).execute()
        c = q.count if hasattr(q, 'count') else len(q.data or [])
        return (c, None)
    except APIError as e:
        return (0, str(e))


def safe_count_jsonb(table: str, jsonb_path: str, value: str) -> tuple:
    try:
        data = supabase.table(table).select("id").eq(jsonb_path, value).execute()
        return (len(data.data or []), None)
    except APIError as e:
        return (0, str(e))


def safe_update(table: str, column: str, old: str, new: str) -> tuple:
    try:
        data = supabase.table(table).select("id").eq(column, old).execute()
        ids = [r["id"] for r in (data.data or [])]
        if ids:
            supabase.table(table).update({column: new}).eq(column, old).execute()
        return (len(ids), None)
    except APIError as e:
        return (0, str(e))


def safe_update_jsonb(table: str, jsonb_path: str, old: str, new: str) -> tuple:
    try:
        return (update_jsonb(table, jsonb_path, old, new), None)
    except APIError as e:
        return (0, str(e))


def migrate_all(dry_run: bool = False):
    print(f"\n{'='*60}")
    print(f"  CHURCH → ASHRAYA Migration")
    print(f"  Mode: {'DRY RUN (no writes)' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    results = []

    steps = [
        ("projects", "org_tag", False),
        ("projects", "name", False),
        ("tasks", "org_tag", False),
        ("graph_nodes", "metadata->>org_tag", True),
        ("graph_nodes", "metadata->>entity", True),
        ("raw_dumps", "metadata->>entity", True),
        ("resources", "category", False),
    ]

    for table, col, is_jsonb in steps:
        if is_jsonb:
            count, err = safe_count_jsonb(table, col, OLD_TAG)
        else:
            count, err = safe_count(table, col, OLD_TAG)

        if err:
            print(f"  ⚠ {table}.{col}: skipped ({err})")
            results.append((table, col, "❌ skipped"))
            continue

        results.append((table, col, count))

        if not dry_run and count > 0:
            if is_jsonb:
                updated, update_err = safe_update_jsonb(table, col, OLD_TAG, NEW_TAG)
            else:
                updated, update_err = safe_update(table, col, OLD_TAG, NEW_TAG)
            if update_err:
                print(f"  ✗ {table}.{col}: update failed ({update_err})")
            else:
                print(f"  ✓ {table}.{col}: {updated} rows updated")
        elif dry_run:
            if count > 0 or err:
                pass
            print(f"  ~ {table}.{col}: {count} rows {'would be updated' if dry_run else 'up to date'}{' (name: Church → Ashraya)' if col == 'name' and count > 0 else ''}")
        else:
            print(f"  — {table}.{col}: {count} rows (up to date)")

    print(f"\n{'='*60}")
    total = sum(r[2] for r in results if isinstance(r[2], int))
    if dry_run:
        print(f"  DRY RUN COMPLETE — {total} total records would be affected.")
        print(f"  Run without --dry-run to apply.")
    else:
        print(f"  MIGRATION COMPLETE — {total} total records updated.")
    print(f"{'='*60}\n")

    for table, column, count in results:
        if isinstance(count, int) and count > 0:
            print(f"  ✓ {table}.{column}: {count} rows")
        elif isinstance(count, int):
            print(f"  — {table}.{column}: {count} rows")
        else:
            print(f"  ⚠ {table}.{column}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate CHURCH tag to ASHRAYA")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()
    migrate_all(dry_run=args.dry_run)
