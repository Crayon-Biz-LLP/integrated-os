#!/usr/bin/env python3
"""
migrate_danny_to_tenant1.py — attribute all existing rows to tenant #1 (M0).

Backfills owner_id = Danny's user id on every table that has an owner_id
column (added by db/78_tenant_scoping.sql), then applies the FINALIZE step:
owner_id SET NOT NULL on required-scope tables (audit/meta tables stay
NULLable — attribution only).

Idempotent and safe:
  - dry-run by default (prints per-table totals / untagged counts)
  - --apply writes, then runs verification
  - --verify-only runs verification against an already-migrated DB

Usage:
    python scripts/migrate_danny_to_tenant1.py [--dsn ...] [--apply] [--verify-only]

Connection: --dsn overrides (local copy DB, e.g.
postgresql://postgres@localhost:5433/rhodey_restore_test); otherwise
discovered from .env like scripts/backup_supabase.py.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Tables where owner_id is attribution-only (no NOT NULL finalize).
NULLABLE_ATTRIBUTION = {"audit_logs", "system_audit_logs", "model_registry"}

DANNY_NAME = "Danny"


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


def _conn() -> tuple[str, str | None]:
    if args.dsn:
        return args.dsn, None
    from backup_supabase import discover_conn
    return discover_conn()


def _psql(sql: str, dsn: str, password: str | None) -> str:
    env = {**os.environ}
    if password:
        env["PGPASSWORD"] = password
    r = subprocess.run(
        [_psql_bin(), dsn, "-tAc", sql], env=env, capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise SystemExit(f"❌ psql failed:\n{r.stderr[-2000:]}")
    return r.stdout.strip()


def owner_tables(dsn: str, password: str | None) -> list[str]:
    """All public tables that have an owner_id column (post-78)."""
    out = _psql(
        "select table_name from information_schema.columns "
        "where table_schema='public' and column_name='owner_id' order by table_name",
        dsn, password,
    )
    return [t.strip() for t in out.splitlines() if t.strip()]


def is_nullable(table: str, dsn: str, password: str | None) -> bool:
    out = _psql(
        "select is_nullable from information_schema.columns "
        f"where table_schema='public' and table_name='{table}' and column_name='owner_id'",
        dsn, password,
    )
    return out == "YES"


def stats(table: str, dsn: str, password: str | None) -> tuple[int, int]:
    """(total_rows, tagged_rows) for owner_id."""
    out = _psql(
        f"select count(*) || '|' || count(owner_id) from public.{table}", dsn, password,
    )
    total, _, tagged = out.partition("|")
    return int(total or 0), int(tagged or 0)


def verify(uid: str, dsn: str, password: str | None) -> bool:
    tables = owner_tables(dsn, password)
    ok = True
    print("\n🔎 Verification")
    for t in tables:
        total, tagged = stats(t, dsn, password)
        untagged = total - tagged
        required = t not in NULLABLE_ATTRIBUTION
        status = "✅" if (untagged == 0 and (not required or not is_nullable(t, dsn, password))) else "⚠️"
        if untagged > 0 or (required and is_nullable(t, dsn, password)):
            ok = False
        print(f"  {status} {t}: {tagged}/{total} attributed" + ("" if required else " (nullable)"))

    # core_config uniqueness by (owner_id, key)
    dupes = _psql(
        "select count(*) from (select owner_id, key from public.core_config "
        "group by owner_id, key having count(*) > 1) d",
        dsn, password,
    )
    print(f"  {'✅' if dupes == '0' else '⚠️'} core_config duplicate (owner_id,key) rows: {dupes}")
    if dupes != "0":
        ok = False

    # users row present
    user = _psql("select id from public.users where name = 'Danny' limit 1", dsn, password)
    print(f"  {'✅' if user else '⚠️'} tenant #1 (Danny) users row: {user or 'MISSING'}")
    if not user:
        ok = False
    return ok


def main() -> None:
    global args
    parser = argparse.ArgumentParser(description="Backfill owner_id → tenant #1 (Danny)")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--apply", action="store_true", help="Write + finalize (default dry-run)")
    parser.add_argument("--verify-only", action="store_true", help="Run verification only")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")  # needed when --dsn is not given (env discovery)
    dsn, password = _conn()
    print(f"🔌 target: {dsn.split('@')[-1].split('?')[0]}")

    # sanity: schema migrated?
    tables = owner_tables(dsn, password)
    if not tables:
        raise SystemExit("❌ No tables have owner_id — run db/78_tenant_scoping.sql first.")
    users_exists = _psql("select to_regclass('public.users')", dsn, password)
    if not users_exists:
        raise SystemExit("❌ public.users missing — run db/78_tenant_scoping.sql first.")

    from bootstrap_tenant import ensure_user
    # apply-gated: a dry-run must NOT write the users row either. M4: persist
    # Danny's telegram chat from env so his channel survives once a second
    # active user exists (the resolver's env fallback is single-user-only).
    uid = ensure_user(
        DANNY_NAME, dsn, password, apply=args.apply,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )
    print(f"👤 tenant #1: {DANNY_NAME} → {uid}")

    if args.verify_only:
        sys.exit(0 if verify(uid, dsn, password) else 1)

    required = [t for t in tables if t not in NULLABLE_ATTRIBUTION]
    print(f"\n📋 {len(tables)} tables with owner_id ({len(required)} required, "
          f"{len(NULLABLE_ATTRIBUTION)} nullable-attribution)")

    # ── dry-run plan ──
    print("\n📊 Current state (dry-run):")
    for t in tables:
        total, tagged = stats(t, dsn, password)
        mark = " " if tagged == total else " ← needs backfill"
        print(f"  {t}: {tagged}/{total}{mark}")

    if not args.apply:
        print("\n(dry-run — pass --apply to backfill + finalize)")
        return

    # ── apply ──
    print("\n⚙️  Backfilling owner_id …")
    for t in tables:
        total, tagged = stats(t, dsn, password)
        if tagged < total:
            _psql(f"update public.{t} set owner_id = '{uid}' where owner_id is null", dsn, password)
            print(f"  {t}: backfilled {total - tagged} rows")

    print("\n🔒 Finalizing NOT NULL on required tables …")
    for t in required:
        if is_nullable(t, dsn, password):
            # guard: only if no NULLs remain (should hold after backfill)
            _psql(f"alter table public.{t} alter column owner_id set not null", dsn, password)
            print(f"  {t}: owner_id SET NOT NULL")

    ok = verify(uid, dsn, password)
    print("\n✅ Migration complete — all rows attributed to tenant #1." if ok
          else "\n⚠️ Verification found issues — review output above.")
    if ok:
        print("\nNext step: seed tenant #1's M6 config rows so his values live in")
        print("config, not fallback code — run:")
        print("    python scripts/seed_tenant1_m6_config.py --dsn <dsn> --apply")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
