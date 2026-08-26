#!/usr/bin/env python3
"""
seed_test_tenant_mirror.py — scenario world-builder for the Test tenant.

Seeds a realistic, fully-fictional world into the Test tenant
(users.name = 'Test') so emulator UATs exercise real pipeline behavior:

  entities          — orgs (incl. sub-org hierarchy), persons (incl.
                      surname-collision pairs), places/events/emotional
                      states, plus filler nodes to push past PostgREST's
                      1000-row page cap (reproduces the Aug 25 truncation
                      class in match_existing_nodes / edge auto-approval)
  briefing_sentinel — tasks across time windows with work/personal org mix,
                      overdue + due-soon pressure, an IST-midnight edge case,
                      and a same-slot event pair for conflict detection
  decisions         — approve/reject/snooze/correction history for the
                      learning loop

Safety:
  - Writes ONLY rows with owner_id = Test tenant id.
  - Every seeded row carries metadata.seed_tag = 'mirror_v1' (graph_nodes)
    or source = 'test_seed' + metadata.seed_tag (tasks) for surgical cleanup.
  - Natural labels (no [TEST] prefix) on purpose: suggestion-card chips must
    read realistically. Cleanup is owner-scoped — see
    scripts/cleanup_test_tenant_mirror.py.
  - Idempotent: refuses to double-seed unless --force (pair --force with a
    prior cleanup run; plain re-runs duplicate rows).
  - Dry-run by default — pass --apply to write.

Usage:
    python scripts/seed_test_tenant_mirror.py [--scenario all|entities|briefing_sentinel|decisions]
        [--apply] [--force]
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TEST_OWNER_ID = "e87f0279-3ec0-4875-af69-49894ee9da6f"  # users.name = 'Test'
SEED_TAG = "mirror_v1"
FILLER_COUNT = 1200  # filler mass to cross the PostgREST 1000-row cap

# ---------------------------------------------------------------------------
# The fictional world (shape-mirrors Danny's tenant, names fictional)
# ---------------------------------------------------------------------------

# (name, keywords, is_personal) — keywords feed user_orgs mirror below
ORGS = [
    ("Nordlicht", ["nordlicht", "rollout", "client"], False),
    ("Tidewell", ["tidewell", "os", "product"], False),
    ("Prismwork", ["prismwork", "governance", "compliance"], False),
    ("Axionly", ["axionly", "middleware", "platform"], False),
    ("Cobalt & Finch", ["cobalt", "finch", "legal"], False),
    ("Larkspur Bank", ["larkspur", "bank", "reconciliation"], False),
    ("Vantage Hotels", ["vantage", "hotel", "hospitality"], False),
    ("Solstice Labs", ["solstice", "labs", "research"], False),
    # Personal cluster with sub-org hierarchy (mirrors the Ashraya tree)
    ("Havenlight", ["havenlight", "ministry", "operations", "accounts", "pastor"], True),
    ("Havenlight Metro East", [], True),
    ("Havenlight Metro Central", [], True),
    ("Havenlight Metro West", [], True),
    ("Havenlight National", [], True),
    ("Personal", ["personal", "home", "family", "bills", "finances", "prayer"], True),
]

# UAT fixture: the "new org" analog of Project Balance. Deliberately NOT in
# ORGS/user_orgs and NOT seeded as a node — a UAT message mentioning it must
# surface it as New/Pending and create it via the confirm flow.
UAT_NEW_ORG = "Meridian Group"

# Persons; collision pairs included deliberately (shared surname stems /
# shared first names) to stress fuzzy matching without cross-matching.
PERSONS = [
    # Meridian Group fixture people (the David/Stacey/Lanette/Edward analogs)
    "Elena Vasquez", "Tom Okafor", "Lisa Chen", "Raj Iyer",
    # Collision pair 1: shared surname stem (the Quantson/Robinson disease)
    "Daniel Whitfield", "Daniel Whitmore",
    # Collision pair 2
    "Priya Sharma", "Priya Sharman",
    # Collision pair 3
    "Arjun Nair", "Arjun Nairi",
    # Collision pair 4
    "Marcus Webb", "Marcus Webster",
    # Shared first names across different orgs
    "John Mathew", "John Varghese", "Sarah Thomas", "Sarah Kurian",
    # Plain roster
    "Anita Desai", "Vikram Bose", "Meera Krishnan", "Farhan Ali",
    "Grace Mathew", "Reuben Pillai", "Tanvi Reddy", "Owen Hartley",
    "Nadia Rahman", "Peter Sundar", "Leah Verghese",
]

PLACES = ["Bandra Office", "Metro Central Cafe", "Larkspur Branch",
          "Havenlight Hall", "Vantage Residency", "Solstice Campus"]
EVENTS = ["Quarterly Review", "Board Call", "Volunteer Day",
          "Product Demo", "Budget Workshop", "Townhall"]
EMOTIONAL_STATES = ["Focused", "Drained", "Optimistic"]

# user_orgs settings mirror — same shape as production. Meridian Group is
# deliberately excluded: it must behave as a NEW org during UAT exactly like
# Project Balance did.
USER_ORGS = [
    {"name": name, "keywords": kw, "is_personal": personal}
    for name, kw, personal in ORGS
]

# (title, org_or_None, status, deadline_offset_days, priority)
TASKS = [
    # --- briefing: today ---
    ("Review Nordlicht rollout plan", "Nordlicht", "open", 0, "high"),
    ("Send Larkspur Bank reconciliation", "Larkspur Bank", "open", 0, "medium"),
    ("Family insurance renewal paperwork", "Personal", "open", 0, "low"),
    # --- briefing: tomorrow ---
    ("Prismwork compliance call prep", "Prismwork", "open", 1, "high"),
    ("Havenlight Metro East volunteer roster", "Havenlight Metro East", "open", 1, "medium"),
    # --- briefing: this week ---
    ("Draft Axionly platform brief", "Axionly", "open", 3, "medium"),
    ("Vantage Hotels Q3 review deck", "Vantage Hotels", "open", 4, "medium"),
    ("Havenlight National accounts summary", "Havenlight National", "open", 5, "low"),
    # --- briefing: completed recently ---
    ("Solstice Labs intro call", "Solstice Labs", "completed", -2, "medium"),
    ("Cobalt & Finch contract markup", "Cobalt & Finch", "completed", -3, "high"),
    # --- sentinel: OVERDUE (negative deadlines) ---
    ("Overdue: Tidewell invoice follow-up", "Tidewell", "open", -2, "high"),
    ("Overdue: Havenlight Metro Central maintenance", "Havenlight Metro Central", "open", -4, "medium"),
    # --- sentinel: due soon (gets a reminder via UPDATE below) ---
    ("Due soon: submit Prismwork audit form", "Prismwork", "open", 0, "high"),
    # --- IST-midnight edge: reminder lands just after midnight IST ---
    ("Midnight-edge: Nordlicht status ping", "Nordlicht", "open", 1, "low"),
]


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _node_values() -> list[str]:
    """graph_nodes value tuples for all entity nodes (NO Meridian Group)."""
    meta = json.dumps({"seed_tag": SEED_TAG})
    out = []
    for name, _, _ in ORGS:
        out.append(f"('{TEST_OWNER_ID}', {_q(name)}, 'organization', {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    for p in PERSONS:
        out.append(f"('{TEST_OWNER_ID}', {_q(p)}, 'person', {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    for p in PLACES:
        out.append(f"('{TEST_OWNER_ID}', {_q(p)}, 'place', {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    for e in EVENTS:
        out.append(f"('{TEST_OWNER_ID}', {_q(e)}, 'event', {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    for e in EMOTIONAL_STATES:
        out.append(f"('{TEST_OWNER_ID}', {_q(e)}, 'emotional_state', {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    return out


def _filler_values(count: int) -> list[str]:
    """Bulk mass of memory/task-type nodes — invisible to matching after the
    Aug 25 type-filter fix, but reproduces the pre-fix cap condition."""
    rng = random.Random(42)
    meta = json.dumps({"seed_tag": SEED_TAG})
    out = []
    for i in range(count):
        if rng.random() < 0.75:
            label, t = f"Memory_{5000 + i}", "memory"
        else:
            label = f"Follow up {rng.choice(PERSONS)} on {rng.choice(ORGS)[0]} item {i}"
            t = "task"
        out.append(f"('{TEST_OWNER_ID}', {_q(label)}, {t!r}, {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())".replace("'", "'", 0).join([""] * 2)
                   if False else
                   f"('{TEST_OWNER_ID}', {_q(label)}, {_q(t)}, {_q(meta)}::jsonb, "
                   f"'asserted', true, 1, now())")
    return out


def scenario_entities() -> list[str]:
    stmts: list[str] = []
    rows = _node_values() + _filler_values(FILLER_COUNT)

    stmts.append(
        "INSERT INTO graph_nodes (owner_id, label, type, metadata, epistemic_status, "
        "is_current, version, last_referenced_at) VALUES\n  "
        + ",\n  ".join(rows) + ";"
    )

    stmts.append(
        f"UPDATE user_settings SET user_orgs = {_q(json.dumps(USER_ORGS))}::jsonb "
        f"WHERE user_id = '{TEST_OWNER_ID}';"
    )
    return stmts


def scenario_briefing_sentinel() -> list[str]:
    values = []
    for title, org, status, ddl, prio in TASKS:
        deadline_sql = f"DATE '{(date.today() + timedelta(days=ddl)).isoformat()}'"
        org_sql = (f"(SELECT id FROM graph_nodes WHERE owner_id = '{TEST_OWNER_ID}' "
                   f"AND type = 'organization' AND is_current AND label = {_q(org)})") if org else "NULL"
        values.append(
            f"('{TEST_OWNER_ID}', {_q(title)}, {_q(status)}, {_q(prio)}, {deadline_sql}, "
            f"now(), NULL, {_q('test_seed')}, {org_sql})"
        )

    # NOTE: tasks has no metadata column — cleanup keys on source='test_seed'.
    stmts = [
        "INSERT INTO tasks (owner_id, title, status, priority, deadline, created_at, "
        "reminder_at, source, organization_id) VALUES\n  "
        + ",\n  ".join(values) + ";",
        # Sentinel: due-soon reminder in the next few hours.
        f"UPDATE tasks SET reminder_at = now() + interval '6 hours' "
        f"WHERE owner_id = '{TEST_OWNER_ID}' AND source = 'test_seed' "
        f"AND title LIKE 'Due soon:%';",
        # IST-midnight edge: reminder at 00:30 IST.
        f"UPDATE tasks SET reminder_at = "
        f"(date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') + interval '30 minutes') "
        f"AT TIME ZONE 'Asia/Kolkata' "
        f"WHERE owner_id = '{TEST_OWNER_ID}' AND source = 'test_seed' "
        f"AND title LIKE 'Midnight-edge:%';",
    ]
    return stmts


def scenario_decisions() -> list[str]:
    rows = [
        ("suggestion_approved", "Approved creation of org node 'Meridian Group'", 0.95),
        ("suggestion_rejected", "Rejected duplicate person merge 'John V.' into 'John Mathew'", 0.90),
        ("suggestion_snoozed", "Snoozed 'Draft Axionly platform brief' until next week", 0.85),
        ("correction", "Corrected org linkage: task belongs to Nordlicht, not Tidewell", 1.00),
        ("graph_node_merge", "Merged pending 'Havenlight Metro' into 'Havenlight Metro Central'", 1.00),
        ("task_completed", "Completed 'Solstice Labs intro call' via quick confirmation", 0.98),
    ]
    vals = [
        f"('{TEST_OWNER_ID}', {_q(dt)}, {_q(title)}, 'emulator_uat', 'active', {conf:.2f}, "
        f"now() - interval '{i * 7 + 1} days', now(), false)"
        for i, (dt, title, conf) in enumerate(rows)
    ]
    return ["INSERT INTO decisions (owner_id, decision_type, title, source, status, "
            "confidence, decided_at, updated_at, auto_decided) VALUES\n  "
            + ",\n  ".join(vals) + ";"]


SCENARIOS = {
    "entities": scenario_entities,
    "briefing_sentinel": scenario_briefing_sentinel,
    "decisions": scenario_decisions,
}


def _psql_path() -> str:
    found = shutil.which("psql")
    if not found:
        raise SystemExit("❌ psql not found on PATH (brew install libpq)")
    return found


def _discover_conn() -> tuple[list[str], dict]:
    """Return (psql_conn_args, env) using backup_supabase's discovery."""
    from backup_supabase import discover_conn
    dsn, password = discover_conn()
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    return [dsn], env


def already_seeded(conn_args: list[str], env: dict) -> int:
    res = subprocess.run(
        [_psql_path(), *conn_args, "-t", "-A", "-c",
         f"SELECT count(*) FROM graph_nodes WHERE owner_id = ''{TEST_OWNER_ID}'' "
         f"AND metadata->>'seed_tag' = '{SEED_TAG}';"],
        text=True, env=env, capture_output=True)
    return int(res.stdout.strip() or 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Test tenant with fictional OS scenarios",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write (default: dry run)")
    parser.add_argument("--force", action="store_true",
                        help="Seed even if rows tagged '%s' exist" % SEED_TAG)
    args = parser.parse_args()

    conn_args, env = _discover_conn()
    existing = already_seeded(conn_args, env)
    if existing and not args.force:
        print(f"ℹ️  {existing} graph rows already tagged '{SEED_TAG}'. "
              f"Run scripts/cleanup_test_tenant_mirror.py first, or use --force.")
        sys.exit(0 if existing else 1)

    wanted = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    stmts: list[str] = []
    for name in wanted:
        generated = SCENARIOS[name]()
        stmts.extend(generated)
        print(f"  • {name}: {len(generated)} statement(s)")

    sql = ("BEGIN;\nSET LOCAL statement_timeout = '60s';\n"
           + "\n".join(stmts) + "\nCOMMIT;")

    if args.apply:
        res = subprocess.run([_psql_path(), *conn_args, "-v", "ON_ERROR_STOP=1"],
                             input=sql, text=True, env=env, capture_output=True)
        if res.returncode != 0:
            print(res.stderr, file=sys.stderr)
            raise SystemExit("❌ Seed SQL failed — transaction rolled back")
        print("✅ Seeded. Verify:")
        print(f"   SELECT type, count(*) FROM graph_nodes "
              f"WHERE owner_id = ''{TEST_OWNER_ID}'' GROUP BY 1 ORDER BY 1;")
    else:
        print("── DRY RUN — first statement preview ──")
        first = stmts[0]
        lines = first.splitlines()
        print("\n".join(lines[:12]))
        print(f"... ({len(lines)} lines in this statement, "
              f"{len(stmts)} statements total). Pass --apply to write.")


if __name__ == "__main__":
    main()
