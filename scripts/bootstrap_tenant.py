#!/usr/bin/env python3
"""
bootstrap_tenant.py — create a tenant (M0, plans/69-multi-tenant-product-plan.md).

Creates, idempotently:
  1. a `users` row (the tenant)
  2. a `user_settings` row (timezone, domains, personal_orgs, voice, context —
     seeded from core/services/user_settings.py defaults unless overridden)
  3. a best-effort root graph node (the person's "me" node)

Usage:
    python scripts/bootstrap_tenant.py --name "Priya" [--email p@example.com] \\
        [--timezone "Asia/Kolkata"] [--dsn postgresql://...] [--apply]

Connection:
    --dsn overrides (use for the local restore copy, e.g.
    postgresql://postgres@localhost:5433/rhodey_restore_test).
    Without --dsn, connection is discovered from .env the same way
    scripts/backup_supabase.py does (pooler/direct with PGPASSWORD).

Safety: dry-run by default — pass --apply to write.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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


def _conn() -> tuple[str, str | None]:
    """Return (dsn, password_for_pgpassword)."""
    if args.dsn:
        return args.dsn, None
    from backup_supabase import discover_conn  # sibling script, same dir
    return discover_conn()


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


def ensure_user(
    name: str, dsn: str, password: str | None,
    email: str | None = None, api_key: str | None = None, apply: bool = True,
    telegram_chat_id: str | None = None,
) -> str:
    """Return the user id for `name`, creating the row when missing (idempotent).

    When api_key is given, stores its SHA-256 hash (users.api_key_hash) so
    the user can authenticate the app with that key (M1).

    telegram_chat_id (M4): persists the tenant's Telegram channel. This is
    what keeps tenant #1 (Danny) working when user #2 arrives — the env
    fallback in resolve_telegram_chat_id() ONLY applies while a single
    active user exists, so without this backfill Danny's Telegram nudges
    would silently stop the moment a second user is added.
    """
    api_hash = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
    existing = _psql(
        f"select id from public.users where name = {_lit(name)} limit 1", dsn, password,
    )
    if existing:
        if (api_hash or telegram_chat_id) and apply:
            sets = []
            if api_hash:
                sets.append(f"api_key_hash = {_lit(api_hash)}")
            if telegram_chat_id:
                sets.append(f"telegram_chat_id = {_lit(telegram_chat_id)}")
            _psql(
                f"update public.users set {', '.join(sets)} where id = '{existing}'",
                dsn, password,
            )
            print(f"  user row updated for '{name}' (api key / telegram chat)")
        return existing
    uid = str(uuid.uuid4())
    print(f"  user '{name}' not found → creating{'' if apply else ' (dry-run, would create)'}")
    if not apply:
        return uid  # simulate
    cols, vals = ["id", "name"], [f"'{uid}'", _lit(name)]
    if email:
        cols.append("email")
        vals.append(_lit(email))
    if api_hash:
        cols.append("api_key_hash")
        vals.append(_lit(api_hash))
    if telegram_chat_id:
        cols.append("telegram_chat_id")
        vals.append(_lit(telegram_chat_id))
    _psql(
        f"insert into public.users ({', '.join(cols)}) values ({', '.join(vals)})",
        dsn, password,
    )
    return uid


def _lit(value: str) -> str:
    """Single-quote a literal for SQL (escape embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    global args
    parser = argparse.ArgumentParser(description="Create a tenant (users + settings + root node)")
    parser.add_argument("--name", required=True, help="Tenant display name (e.g. Priya)")
    parser.add_argument("--email", default=None)
    parser.add_argument("--timezone", default="Asia/Kolkata", help="IANA timezone")
    parser.add_argument("--context", default=None, help="One-line 'who they are' for prompt slots (M2)")
    parser.add_argument("--domains", default=None,
                        help="JSON list of {name, keywords} routing domains (M2; defaults to Danny's life domains)")
    parser.add_argument("--personal-orgs", default=None,
                        help="JSON list of personal/life org names for the work-life split (M2)")
    parser.add_argument("--api-key", default=None,
                        help="Per-user API key for the app (stored as SHA-256 hash; printed once)")
    parser.add_argument("--dsn", default=None, help="Override connection (local copy DB)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default is dry-run)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")  # needed when --dsn is not given (env discovery)
    dsn, password = _conn()
    print(f"🔌 target: {dsn.split('@')[-1].split('?')[0]}")

    # 1. users row
    uid = ensure_user(args.name, dsn, password, args.email, args.api_key, apply=args.apply)
    print(f"  tenant id: {uid}")
    if args.api_key:
        print(f"  🔑 user API key (enter in app Settings): {args.api_key}")

    # 2. user_settings row
    if args.apply:
        from core.services.user_settings import (
            DEFAULT_CONTEXT, DEFAULT_DOMAINS, DEFAULT_PERSONAL_ORGS,
        )
        domains = args.domains if args.domains is not None else json.dumps(DEFAULT_DOMAINS)
        personal_orgs = (
            args.personal_orgs if args.personal_orgs is not None
            else json.dumps(DEFAULT_PERSONAL_ORGS)
        )
        context = args.context if args.context is not None else DEFAULT_CONTEXT
        _psql(
            "insert into public.user_settings "
            "(user_id, timezone, domains, personal_orgs, voice, context) "
            f"values ('{uid}', {_lit(args.timezone)}, {_lit(domains)}::jsonb, "
            f"{_lit(personal_orgs)}::jsonb, NULL, {_lit(context)}) "
            "on conflict (user_id) do update set timezone = excluded.timezone, "
            "domains = coalesce(excluded.domains, public.user_settings.domains), "
            "personal_orgs = coalesce(excluded.personal_orgs, public.user_settings.personal_orgs), "
            "context = coalesce(excluded.context, public.user_settings.context), updated_at = now()",
            dsn, password,
        )
        print("  user_settings: upserted (timezone, domains, personal_orgs, context)")
    else:
        print("  user_settings: would upsert (dry-run)")

    # 2b. M6 neutral config rows (core_config, on conflict do nothing)
    # A NEW tenant is born neutral: no archive labels/edges, INBOX-only
    # email scan. They are upgraded to their real world by
    # seed_user_world.py (M5 seeding session). CRITICAL: only inserted for
    # brand-new users — an existing tenant (e.g. re-running bootstrap for
    # tenant #1 to add an API key) must keep its rows / legacy fallbacks.
    # An unconditional empty 'email_archive_label' row for Danny would be
    # treated as authoritative by the M6 reader and silently drop his
    # 'Completed/Ashraya' label filter.
    existing_uid = _psql(
        f"select id from public.users where name = {_lit(args.name)} limit 1",
        dsn, password,
    )
    created_now = not existing_uid
    if created_now:
        if args.apply:
            _psql(
                "insert into public.core_config (key, content, owner_id) values "
                f"('email_archive_label', '', '{uid}'), "
                f"('archive_person_labels', '[]', '{uid}'), "
                f"('archive_org_labels', '[]', '{uid}'), "
                f"('archive_edge_rules', '[]', '{uid}'), "
                f"('archive_root_label', '', '{uid}') "
                "on conflict (owner_id, key) do nothing",
                dsn, password,
            )
            print("  core_config: neutral M6 rows ensured (new tenant)")
        else:
            print("  core_config: would ensure neutral M6 rows (dry-run, new tenant)")
    else:
        print("  core_config: existing tenant — M6 rows left untouched (rows/fallbacks preserved)")

    # 3. best-effort root graph node (their 'me' node)
    try:
        from core.lib.graph_rules import normalize_label  # repo util
        norm = normalize_label(args.name)
    except Exception:
        norm = args.name.lower().strip()
    if args.apply:
        # Owner-scoped existence check FIRST — never adopt another tenant's
        # node. If a same-label node exists under a different owner, the
        # insert below fails on the (normalized_label, type) unique constraint,
        # which is the correct outcome (we must not steal it).
        exists = _psql(
            "select id from public.graph_nodes "
            f"where owner_id = '{uid}' and normalized_label = {_lit(norm)} "
            "and type = 'person' limit 1",
            dsn, password,
        )
        if exists:
            print(f"  root graph node '{args.name}' (person): already exists")
        else:
            # read NOT NULL defaults defensively, then insert
            defaults = {}
            rows = _psql(
                "select column_name, column_default from information_schema.columns "
                "where table_schema='public' and table_name='graph_nodes' and is_nullable='NO'",
                dsn, password,
            )
            for line in rows.splitlines():
                col, _, default = line.partition("|")
                defaults[col.strip()] = default.strip()
            eps = defaults.get("epistemic_status") or "'active'"
            refc = defaults.get("reference_count") or "0"
            isc = defaults.get("is_current") or "true"
            ver = defaults.get("version") or "1"
            try:
                _psql(
                    "insert into public.graph_nodes (label, type, normalized_label, metadata, "
                    f"epistemic_status, reference_count, is_current, version, owner_id) "
                    f"values ({_lit(args.name)}, 'person', {_lit(norm)}, "
                    f"'{{\"source\": \"bootstrap\", \"role\": \"user\"}}'::jsonb, "
                    f"{eps}, {refc}, {isc}, {ver}, '{uid}')",
                    dsn, password,
                )
                print(f"  root graph node '{args.name}' (person): created")
            except SystemExit as e:
                print(f"  ⚠️ root node skipped (non-fatal — likely label owned by another tenant): {e}")
    else:
        print("  root graph node: would upsert (dry-run)")

    print("\n✅ Done." if args.apply else "\n(dry-run — pass --apply to write)")


if __name__ == "__main__":
    main()
