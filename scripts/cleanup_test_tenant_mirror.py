#!/usr/bin/env python3
"""
cleanup_test_tenant_mirror.py — reverse of seed_test_tenant_mirror.py.

Modes:
  --seeded-only (default) — delete ONLY rows this seeder created
      (graph_nodes.metadata.seed_tag = 'mirror_v1', tasks.source = 'test_seed',
       decisions.source = 'emulator_uat') and restore
      user_settings.user_orgs to '[]'.
  --all — full sandbox reset: additionally wipes ALL graph_nodes / tasks /
      memories / pending_nodes / pending_graph_edges rows owned by the Test
      tenant (the whole tenant is a sandbox; nothing else may live there).
      Also removes legacy junk from earlier UAT rounds.

Safety: dry-run by default — pass --apply to write. Never touches any other
tenant (every statement is pinned to TEST_OWNER_ID).

Usage:
    python scripts/cleanup_test_tenant_mirror.py [--all] [--apply]
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TEST_OWNER_ID = "e87f0279-3ec0-4875-af69-49894ee9da6f"  # users.name = 'Test'
SEED_TAG = "mirror_v1"


def _psql_path() -> str:
    found = shutil.which("psql")
    if not found:
        raise SystemExit("❌ psql not found on PATH (brew install libpq)")
    return found


def _discover_conn() -> tuple[list[str], dict]:
    from backup_supabase import discover_conn
    dsn, password = discover_conn()
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    return [dsn], env


def seeded_statements() -> list[str]:
    tag = f"metadata->>'seed_tag' = '{SEED_TAG}'"
    return [
        # Confirm-flow artifacts created during UAT against seeded entities
        # (pending nodes/edges referencing our world) — scoped by owner only,
        # since they inherit labels but not our metadata.
        f"DELETE FROM pending_graph_edges WHERE owner_id = '{TEST_OWNER_ID}';",
        f"DELETE FROM pending_nodes WHERE owner_id = '{TEST_OWNER_ID}';",
        # FK order: tasks/memories reference graph_nodes — children first
        f"DELETE FROM tasks WHERE owner_id = '{TEST_OWNER_ID}' AND source = 'test_seed';",
        f"DELETE FROM graph_nodes WHERE owner_id = '{TEST_OWNER_ID}' AND {tag};",
        f"UPDATE user_settings SET user_orgs = '[]'::jsonb WHERE user_id = '{TEST_OWNER_ID}';",
    ]


def all_statements() -> list[str]:
    o = f"owner_id = '{TEST_OWNER_ID}'"
    return [
        *seeded_statements(),
        # Full reset: children first (FK order), then all nodes.
        # FK children of graph_nodes (per information_schema): projects,
        # graph_edges, messages, tasks, merge_proposals.
        f"DELETE FROM tasks WHERE {o};",
        f"DELETE FROM memories WHERE {o};",
        f"DELETE FROM merge_proposals WHERE {o};",
        f"DELETE FROM graph_edges WHERE {o};",
        f"DELETE FROM messages WHERE {o};",
        f"DELETE FROM projects WHERE {o};",
        f"DELETE FROM graph_nodes WHERE {o};",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up Test-tenant seed data",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true",
                        help="Full sandbox reset (all Test-tenant rows), not just seeded rows")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write (default: dry run)")
    args = parser.parse_args()

    stmts = all_statements() if args.all else seeded_statements()
    sql = ("BEGIN;\nSET LOCAL statement_timeout = '120s';\n"
           + "\n".join(stmts) + "\nCOMMIT;")

    conn_args, env = _discover_conn()
    if args.apply:
        res = subprocess.run([_psql_path(), *conn_args, "-v", "ON_ERROR_STOP=1"],
                             input=sql, text=True, env=env, capture_output=True)
        if res.returncode != 0:
            print(res.stderr, file=sys.stderr)
            raise SystemExit("❌ Cleanup SQL failed — transaction rolled back")
        print("✅ Cleanup applied.")
    else:
        print(f"── DRY RUN ({len(stmts)} statements, mode={'--all' if args.all else '--seeded-only'}) ──")
        for s in stmts:
            print("  " + s)
        print("Pass --apply to write.")


if __name__ == "__main__":
    main()
