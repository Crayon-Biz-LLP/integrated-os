#!/usr/bin/env python3
"""
seed_tenant1_m6_config.py — seed tenant #1's (Danny's) M6 config rows so his
values live in core_config, not fallback code (M6 de-personalization,
plans/69-multi-tenant-product-plan.md).

Rows written (idempotent upsert, `on conflict (owner_id, key) do update`):

  email_archive_label   = 'Completed/Ashraya'        (email_ingest default)
  archive_person_labels = ["Danny","Sunju","Jaden","Jeffery","The Boys"]
  archive_org_labels    = ["Solvstrat","Crayon","Church"]
  archive_edge_rules    = Danny's world ({root} substitution)
  archive_root_label    = 'Danny'
  entity_mappings       = Danny's full entity→keyword mapping (archive_ingest
                          default — the RICH mapping, not a degraded subset)
  github_owner          = 'Crayon-Biz-LLP'
  github_repo           = 'integrated-os'

The values are IMPORTED from the code's default constants (single source of
truth) — the seeded rows and the runtime fallbacks can never drift. If the
fallbacks ever change, re-run this seed to converge tenant #1's config.

NOTE on precedence: after seeding, the github_owner/github_repo config rows
win over the GITHUB_OWNER/GITHUB_REPO env vars in resolve_github_config()
(resolution order: config → env → default). The seeded values equal the
legacy defaults, so tenant #1's behavior is unchanged unless its deployment
relied on a non-default env value.

Run order on a fresh multi-tenant deploy:
  1. apply db/78_tenant_scoping.sql (adds users, owner_id, core_config PK)
  2. python scripts/migrate_danny_to_tenant1.py --dsn ... --apply
  3. python scripts/seed_tenant1_m6_config.py --dsn ... --apply   ← this

Usage:
    python scripts/seed_tenant1_m6_config.py [--user Danny] [--dsn postgresql://...] [--apply]

Safety: dry-run by default — pass --apply to write.
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
        "/usr/local/opt/postgresql@17/bin/psql",
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


# ── The M6 keys + their values, imported from code (no drift) ──────────────
KEYS = [
    "email_archive_label",
    "archive_person_labels",
    "archive_org_labels",
    "archive_edge_rules",
    "archive_root_label",
    "entity_mappings",
    "github_owner",
    "github_repo",
    "briefing_sections",
    "briefing_schedule",
]


def m6_rows() -> list[dict]:
    """(key, content) pairs for tenant #1, taken from the TENANT1_* constants."""
    from core.skills.archive_ingest import (
        TENANT1_ARCHIVE_EDGE_RULES,
        TENANT1_ARCHIVE_ORG_LABELS,
        TENANT1_ARCHIVE_PERSON_LABELS,
        TENANT1_ARCHIVE_ROOT_LABEL,
        TENANT1_ENTITY_MAPPINGS,
    )
    from core.skills.email_ingest import TENANT1_EMAIL_ARCHIVE_LABEL
    from core.lib.constants import DEFAULT_GITHUB_OWNER, DEFAULT_GITHUB_REPO
    from core.services.briefing_sections import default_briefing_sections_json
    from core.services.briefing_schedule import schedule_for_preset

    return [
        {"key": "email_archive_label", "content": TENANT1_EMAIL_ARCHIVE_LABEL},
        {"key": "archive_person_labels", "content": json.dumps(TENANT1_ARCHIVE_PERSON_LABELS)},
        {"key": "archive_org_labels", "content": json.dumps(TENANT1_ARCHIVE_ORG_LABELS)},
        {"key": "archive_edge_rules", "content": json.dumps(TENANT1_ARCHIVE_EDGE_RULES)},
        {"key": "archive_root_label", "content": TENANT1_ARCHIVE_ROOT_LABEL},
        {"key": "entity_mappings", "content": json.dumps(TENANT1_ENTITY_MAPPINGS)},
        {"key": "github_owner", "content": DEFAULT_GITHUB_OWNER},
        {"key": "github_repo", "content": DEFAULT_GITHUB_REPO},
        {"key": "briefing_sections", "content": default_briefing_sections_json()},
        # M9.7: Danny's exact pre-M9.7 schedule (classic) — the 30-min
        # heartbeat gate reproduces his briefings byte-for-byte.
        {"key": "briefing_schedule", "content": json.dumps(schedule_for_preset("classic"))},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed tenant #1's M6 config rows into core_config")
    parser.add_argument("--user", default="Danny", help="Tenant display name (default: Danny, tenant #1)")
    parser.add_argument("--dsn", default=None, help="Override connection (local copy DB)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default is dry-run)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")  # needed when --dsn is not given (env discovery)
    dsn, password = (args.dsn, None)
    if not dsn:
        from backup_supabase import discover_conn
        dsn, password = discover_conn()
    print(f"🔌 target: {dsn.split('@')[-1].split('?')[0]}")

    # Guard: db/78 must be applied (core_config needs owner_id + (owner_id,key) PK)
    has_owner = _psql(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='core_config' and column_name='owner_id'",
        dsn, password,
    )
    if not has_owner:
        raise SystemExit(
            "❌ core_config.owner_id missing — apply db/78_tenant_scoping.sql "
            "before seeding tenant #1's config rows."
        )
    has_unique = _psql(
        "select 1 from pg_constraint where conrelid = 'public.core_config'::regclass "
        "and contype = 'u' and (conname = 'core_config_owner_key_key' "
        "or pg_get_constraintdef(oid) ilike '%owner_id, key%' "
        "or pg_get_constraintdef(oid) ilike '%key, owner_id%') limit 1",
        dsn, password,
    )
    if not has_unique:
        raise SystemExit(
            "❌ core_config unique (owner_id, key) missing — apply db/78_tenant_scoping.sql "
            "(its PK change) before seeding."
        )

    uid = _psql(
        f"select id from public.users where name = {_lit(args.user)} limit 1",
        dsn, password,
    )
    if not uid:
        raise SystemExit(
            f"❌ No user named '{args.user}' — run migrate_danny_to_tenant1.py first, "
            "or pass --user with the exact name."
        )

    rows = m6_rows()
    print(f"👤 tenant #1: {args.user} → {uid}")
    print(f"📋 {len(rows)} M6 config rows (values imported from code defaults):")
    current = {}
    for key in KEYS:
        got = _psql(
            f"select content from public.core_config where owner_id = '{uid}' and key = {_lit(key)}",
            dsn, password,
        )
        if got:
            current[key] = got
    changed = 0
    for r in rows:
        prev = current.get(r["key"])
        preview = r["content"]
        if len(preview) > 60:
            preview = preview[:60] + "…"
        if prev is None:
            mark = "← new row"
            changed += 1
        elif prev != r["content"]:
            mark = "← will update (admin override will be reset)"
            changed += 1
        else:
            mark = ""
        print(f"   - {r['key']:<22} = {preview}  {mark}")
    if changed == 0:
        print("\nℹ️  All rows already match the code defaults — nothing to change.")
    if not args.apply:
        print("\n(dry-run — pass --apply to upsert into core_config)")
        return

    sql = (
        "insert into public.core_config (key, content, owner_id) values "
        + ", ".join(
            f"({_lit(r['key'])}, {_lit(r['content'])}, '{uid}')" for r in rows
        )
        + " on conflict (owner_id, key) do update set content = excluded.content, updated_at = now()"
    )
    _psql(sql, dsn, password)
    print(f"✅ Seeded {len(rows)} M6 config rows for tenant #1 ({args.user}) — idempotent (re-run safe).")

    # Self-verify: every key now holds exactly the intended value
    bad = []
    for r in rows:
        got = _psql(
            f"select content::text from public.core_config "
            f"where owner_id = '{uid}' and key = {_lit(r['key'])}",
            dsn, password,
        )
        if got != r["content"]:
            bad.append(f"{r['key']}: expected {r['content'][:40]}… got {got[:40]}…")
    if bad:
        raise SystemExit("❌ Verification failed:\n  " + "\n  ".join(bad))
    print("✅ Verified: all rows match the code defaults exactly.")


if __name__ == "__main__":
    main()
