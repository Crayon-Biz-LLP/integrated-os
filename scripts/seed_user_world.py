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


async def seed_world(supabase, uid: str, world: dict) -> dict:
    """Seed a tenant's world (M5). Runs under the caller's tenant scope.

    Returns a summary dict of what was created. Fail-open per section —
    a bad row never aborts the whole seed.
    """
    from core.pulse.graph import create_graph_node_with_db_record
    from core.pulse.tools import create_task_direct

    created = {"people": 0, "organizations": 0, "tasks": 0, "errors": []}

    # ── 1. user_settings (context, domains, personal_orgs, timezone) ──
    try:
        settings = {
            "user_id": uid,
            "timezone": world.get("timezone") or "Asia/Kolkata",
            "context": world.get("context") or "",
            "domains": json.dumps(world.get("domains") or []),
            "personal_orgs": json.dumps(world.get("personal_orgs") or []),
        }
        row = (
            supabase.table("user_settings")
            .upsert(settings, on_conflict="user_id")
            .execute()
        )
        created["settings"] = bool(row.data)
    except Exception as e:
        created["errors"].append(f"settings: {e}")

    # ── 1b. M6 ingest/archive config (per-tenant core_config rows) ──
    # A new tenant gets their OWN archive/ingest config so they never fall
    # back to tenant #1's (Danny's) hardcoded labels/edges. Rows:
    #   archive_person_labels / archive_org_labels — node typing for the
    #       archive ingest (their own people/orgs; empty = generic typing)
    #   archive_edge_rules — custom graph edges from archive text;
    #       seeded empty ([] = neutral, opt-in)
    #   archive_root_label — their own 'me' node label ('' = derive from
    #       user_settings name via resolve_user_name)
    #   email_archive_label — Gmail label to scan past INBOX; '' =
    #       authoritative INBOX-only (the M6 reader treats a present row
    #       as authoritative, empty content = INBOX only)
    #   github_owner / github_repo — Actions dispatch target (optional)
    try:
        people_names = [
            p.get("name", "").strip() for p in (world.get("people") or [])
            if (p.get("name") or "").strip()
        ]
        org_names = [
            o.get("name", "").strip() for o in (world.get("organizations") or [])
            if (o.get("name") or "").strip()
        ]
        root_label = (world.get("root_label") or "").strip()
        config_rows = [
            {"key": "archive_person_labels", "content": json.dumps([root_label] + people_names if root_label else people_names)},
            {"key": "archive_org_labels", "content": json.dumps(org_names)},
            {"key": "archive_edge_rules", "content": "[]"},
            {"key": "archive_root_label", "content": root_label},
            {"key": "email_archive_label", "content": (world.get("email_archive_label") or "").strip()},
        ]
        if (world.get("github_owner") or "").strip():
            config_rows.append({"key": "github_owner", "content": world["github_owner"].strip()})
        if (world.get("github_repo") or "").strip():
            config_rows.append({"key": "github_repo", "content": world["github_repo"].strip()})
        for row in config_rows:
            supabase.table("core_config").upsert(row, on_conflict="owner_id,key").execute()
        created["config_rows"] = len(config_rows)
    except Exception as e:
        created["errors"].append(f"m6_config: {e}")

    # ── 2. People + organizations (graph nodes via the tenant-scoped path) ──
    for p in world.get("people") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        try:
            res = await create_graph_node_with_db_record(
                label=name,
                node_type="person",
                context=(p.get("context") or "").strip(),
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                created["people"] += 1
            else:
                created["errors"].append(f"person {name}: {res.get('message')}")
        except Exception as e:
            created["errors"].append(f"person {name}: {e}")

    for o in world.get("organizations") or []:
        name = (o.get("name") or "").strip()
        if not name:
            continue
        try:
            res = await create_graph_node_with_db_record(
                label=name,
                node_type="organization",
                context=(o.get("context") or "").strip(),
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                created["organizations"] += 1
            else:
                created["errors"].append(f"org {name}: {res.get('message')}")
        except Exception as e:
            created["errors"].append(f"org {name}: {e}")

    # ── 3. Initial board (tasks via create_task_direct) ──
    for t in world.get("tasks") or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        try:
            # Deterministic dedup key (user id + title) so re-running the
            # seed after a partial failure never duplicates tasks.
            dedup_key = f"seed:{uid}:{title.lower().strip()}"[:16]
            res = await create_task_direct(
                title=title,
                organization_name=(t.get("organization") or "").strip() or None,
                priority=(t.get("priority") or "important").lower(),
                deadline=t.get("deadline"),
                notes=f"onboarding_seed: {world.get('context', '')[:200]}",
                dedup_key=dedup_key,
            )
            if res.get("action") in ("created", "skipped"):
                created["tasks"] += 1
            else:
                created["errors"].append(f"task {title}: {res.get('reason')}")
        except Exception as e:
            created["errors"].append(f"task {title}: {e}")

    # ── 4. Onboarding state: seeded ──
    try:
        supabase.table("user_settings").update({"onboarding_state": "seeded"}).eq("user_id", uid).execute()
    except Exception as e:
        created["errors"].append(f"onboarding_state: {e}")

    return created


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
