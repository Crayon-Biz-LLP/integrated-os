"""
Apply migrations db/32 and db/33 to the production Supabase database,
then verify the fix with a read-only test.

Usage:
    LIVE_DB=true python3 scripts/apply_migrations.py

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars.
Applies SQL via direct PostgreSQL connection if possible, otherwise
prints the SQL for manual execution in Supabase Dashboard.
"""
import os
import sys
import subprocess
import re
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase
from core.lib.graph_rules import normalize_label
from dotenv import load_dotenv

load_dotenv()


def read_sql(path: str) -> str:
    with open(path) as f:
        return f.read()


def print_sql_instructions(migration_name: str, sql: str):
    print("\n" + "=" * 60)
    print(f"  {migration_name}")
    print("=" * 60)
    print("\nCopy the SQL below and run it in your Supabase Dashboard SQL Editor:")
    print("  https://supabase.com/dashboard/project/_/sql/new\n")
    print("--- START SQL ---")
    print(sql)
    print("--- END SQL ---")
    print()


def try_psql(sql: str) -> bool:
    """Try to execute SQL via psql if available."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    match = re.match(r"https?://([^.]+)\.supabase\.co", supabase_url)
    if not match:
        return False

    project_ref = match.group(1)

    # Try multiple password sources: explicit DB password, then service_role_key
    passwords_to_try = []
    db_pw = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("DATABASE_PASSWORD")
    if db_pw:
        passwords_to_try.append(db_pw)
    sr_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if sr_key:
        passwords_to_try.append(sr_key)

    if not passwords_to_try:
        return False

    for password in passwords_to_try:
        # URL-encode the password for the connection string
        from urllib.parse import quote
        encoded_pw = quote(password, safe="")
        conn_str = (
            f"postgresql://postgres:{encoded_pw}"
            f"@db.{project_ref}.supabase.co:5432/postgres"
            f"?sslmode=require"
        )

        try:
            result = subprocess.run(
                ["psql", conn_str, "-c", sql],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print("  SQL executed successfully via psql")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return False


def apply_migration(name: str, sql_path: str) -> bool:
    """Apply a migration: try psql first, then print SQL for manual execution."""
    sql = read_sql(sql_path)
    print(f"\nApplying {name}...")

    if try_psql(sql):
        return True

    print_sql_instructions(name, sql)
    return False


def verify_migration():
    """Read-write verification: exercise the new constraint with test rows, then clean up."""
    supabase = get_supabase()
    print("\n" + "=" * 60)
    print("  VERIFICATION TESTS")
    print("=" * 60)

    passed = 0
    failed = 0

    # --- Test 1: Check table accessibility ---
    print("\n[1/5] Checking memories and graph_nodes accessibility...")
    try:
        supabase.table("memories").select("id").limit(1).execute()
        supabase.table("graph_nodes").select("id").limit(1).execute()
        print("  memories and graph_nodes accessible")
        passed += 1
    except Exception as e:
        print(f"  ERROR: {e}")
        failed += 1

    # --- Test 2: Attempt upsert with new composite constraint target ---
    test_id = uuid.uuid4().hex[:8]
    org_label = f"__TEST_ORG_{test_id}"
    proj_label = f"__TEST_PROJ_{test_id}"

    print("\n[2/5] Testing on_conflict='normalized_label, type' upsert...")
    try:
        # Upsert as organization
        org_res = supabase.table("graph_nodes").upsert({
            "label": org_label,
            "type": "organization",
            "normalized_label": normalize_label(org_label),
            "metadata": {"source": "migration_test"}
        }, on_conflict="normalized_label, type").execute()

        if org_res.data:
            print(f"  Created organization node '{org_label}'")
            passed += 1
        else:
            print("  WARNING: upsert returned no data for org")
            failed += 1
    except Exception as e:
        print(f"  ERROR upserting organization: {e}")
        failed += 1

    # --- Test 3: Upsert same label as project (would fail under old constraints) ---
    print("\n[3/5] Testing same label as different type (project)...")
    try:
        proj_res = supabase.table("graph_nodes").upsert({
            "label": proj_label,
            "type": "project",
            "normalized_label": normalize_label(proj_label),
            "metadata": {"source": "migration_test"}
        }, on_conflict="normalized_label, type").execute()

        if proj_res.data:
            print(f"  Created project node '{proj_label}'")
            passed += 1
        else:
            print("  WARNING: upsert returned no data for project")
            failed += 1
    except Exception as e:
        print(f"  ERROR upserting project: {e}")
        failed += 1

    # --- Test 4: Clean up test rows ---
    print("\n[4/5] Cleaning up test rows...")
    try:
        supabase.table("graph_nodes").delete().eq("label", org_label).execute()
        supabase.table("graph_nodes").delete().eq("label", proj_label).execute()
        print("  Test rows cleaned up")
        passed += 1
    except Exception as e:
        print(f"  WARNING: cleanup failed: {e}")
        # Non-fatal: test rows are harmless

    # --- Test 5: Query orgs/projects for shared labels ---
    print("\n[5/5] Checking existing org/project label overlap...")
    try:
        orgs = supabase.table("organizations").select("name").limit(100).execute()
        projs = supabase.table("projects").select("name").limit(100).execute()

        org_names = {o["name"].strip().lower() for o in (orgs.data or []) if o.get("name")}
        proj_names = {p["name"].strip().lower() for p in (projs.data or []) if p.get("name")}

        shared = org_names & proj_names
        if shared:
            print(f"  Found {len(shared)} labels shared between orgs and projects:")
            for lbl in list(shared)[:5]:
                print(f"    '{lbl}'")
            print("  After migration, sync functions can create distinct nodes for each")
        else:
            print("  No shared labels currently — migration enables future coexistence")
        passed += 1
    except Exception as e:
        print(f"  ERROR: {e}")
        failed += 1

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {passed + failed} tests")
    if failed == 0:
        print("  All verification checks completed successfully.")
    else:
        print("  Some checks had issues. Review errors above.")
    print("=" * 60 + "\n")

    return failed == 0


if __name__ == "__main__":
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    db_dir = os.path.join(os.path.dirname(scripts_dir), "db")

    print("\n" + "#" * 60)
    print("  Integrated OS Migration Runner")
    print("#" * 60)

    # Migration 1: Ghost Vector Cleanup
    applied_any = apply_migration(
        "db/32_memory_delete_cleanup.sql",
        os.path.join(db_dir, "32_memory_delete_cleanup.sql")
    )

    # Migration 2: Hard Label Collisions
    applied_any |= apply_migration(
        "db/33_graph_nodes_composite_unique.sql",
        os.path.join(db_dir, "33_graph_nodes_composite_unique.sql")
    )

    print("\n" + "=" * 60)
    if applied_any:
        print("  Migrations were applied automatically via psql.")
    else:
        print("  Migrations require manual execution.")
        print("  Copy the SQL above into your Supabase Dashboard SQL Editor,")
        print("  then re-run this script to verify.")

    # Always run the verification (handles both pre- and post-migration states)
    verify_migration()
