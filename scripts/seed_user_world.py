#!/usr/bin/env python3
"""
seed_user_world.py — onboarding seeding for a NEW tenant (M5).

Turns a short structured "world" description into the tenant's initial
knowledge graph + settings, using the same tenant-scoped primitives the
product uses at runtime (create_graph_node_with_db_record / create_task_direct
/ user_settings upsert). This is the "seeding session" of M5: the admin
captures the onboarding conversation (what the person does, who they work
with, what's on the board) into a world JSON file, and this script builds
the graph + settings so the first briefing has real context.

World JSON shape (seed_world):
{
  "context": "Priya, COO at Acme, Bengaluru.",
  "timezone": "Asia/Kolkata",
  "domains": [
    {"name": "Acme", "keywords": ["acme", "client", "delivery"]},
    {"name": "Personal", "keywords": ["home", "family", "bills"]}
  ],
  "personal_orgs": ["Personal"],
  "root_label": "Priya",  # optional — their 'me' node label for archive ingest
  "email_archive_label": "",  # optional — Gmail label to scan; '' = INBOX only
  "github_owner": "",  # optional — Actions dispatch target
  "github_repo": "",  # optional
  "people": [
    {"name": "Raj", "context": "CTO at Acme"},
    {"name": "Meera", "context": "co-founder"}
  ],
  "organizations": [
    {"name": "Acme", "context": "the company"},
    {"name": "Startup X", "context": "client"}
  ],
  "tasks": [
    {"title": "Prep Q3 board deck", "priority": "high", "organization": "Acme", "deadline": "2026-08-10T09:00:00+05:30"},
    {"title": "Call Meera about hiring plan", "priority": "important"}
  ]
}

Usage:
    python scripts/seed_user_world.py --user "Priya" --world path/to/world.json [--dsn ...] [--apply]

Safety: dry-run by default — pass --apply to write.

The core `seed_world()` function is importable so the verification gate
(scripts/verify_m5_onboarding.py) can run it against the copy DB under a
tenant scope without psql.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _psql_bin() -> str:
    found = shutil.which("psql")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/opt/postgresql@17/bin/psql",
        "/opt/homebrew/opt/libpq/bin/psql",
    ):
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("❌ psql not found on PATH. Install with: brew install libpq (or postgresql@17)")


def _psql(sql: str, dsn: str, password: str | None) -> str:
    env = {**os.environ}
    if password:
        env["PGPASSWORD"] = password
    r = subprocess.run(
        [_psql_bin(), dsn, "-tAc", sql], env=env, capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise SystemExit(f"❌ psql failed:\n{r.stderr[-2000:]}")
    return r.stdout.strip()


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# The seeding engine lives in core/services/seeding.py (shared with the
# in-app onboarding journey). Re-exported here so scripts/verify_m5_onboarding.py
# and any other caller of scripts.seed_user_world.seed_world keep working.
from core.services.seeding import seed_world  # noqa: E402,F401  (used by verify gate via module attr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a new tenant's world (M5 onboarding)")
    parser.add_argument("--user", required=True, help="Tenant display name (must exist in public.users)")
    parser.add_argument("--world", required=True, help="Path to the world JSON file")
    parser.add_argument("--dsn", default=None, help="Override connection (local copy DB)")
    parser.add_argument("--apply", action="store_true", help="Write (default dry-run)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    world_path = Path(args.world)
    if not world_path.exists():
        raise SystemExit(f"❌ World file not found: {world_path}")
    world = json.loads(world_path.read_text())

    dsn, password = (args.dsn, None)
    if not dsn:
        from backup_supabase import discover_conn
        dsn, password = discover_conn()
    print(f"🔌 target: {dsn.split('@')[-1].split('?')[0]}")

    uid = _psql(
        f"select id from public.users where name = {_lit(args.user)} limit 1",
        dsn, password,
    )
    if not uid:
        raise SystemExit(f"❌ No user named '{args.user}' — create them first via scripts/bootstrap_tenant.py")

    if not args.apply:
        print(f"👤 tenant: {args.user} → {uid}")
        print(f"📋 world: {len(world.get('people') or [])} people, "
              f"{len(world.get('organizations') or [])} orgs, "
              f"{len(world.get('tasks') or [])} tasks")
        print("\n(dry-run — pass --apply to seed the graph + settings)")
        return

    import asyncio
    from core.services.db import tenant_aware_client, tenant_scope

    async def _seed_async(uid_: str, w: dict):
        with tenant_scope(uid_):
            supabase = tenant_aware_client()
            return await seed_world(supabase, uid_, w)

    result = asyncio.run(_seed_async(uid, world))
    print(f"✅ Seeded {result.get('people', 0)} people, "
          f"{result.get('organizations', 0)} orgs, "
          f"{result.get('tasks', 0)} tasks for {args.user}")
    if result.get("errors"):
        print("⚠️ Partial errors:")
        for err in result["errors"][:10]:
            print(f"  - {err}")


if __name__ == "__main__":
    main()
