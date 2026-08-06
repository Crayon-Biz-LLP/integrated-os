#!/usr/bin/env python3
"""
Take a full logical backup of the production Supabase database.

Saves timestamped dumps into backups/:
  - rhodey-full-<ts>.dump      (custom format, full DB — primary restore artifact)
  - rhodey-public-<ts>.sql     (plain SQL, public schema only — inspectable)

Uses the same connection logic as scripts/apply_migrations.py and
core/services/async_db.py: direct connection to db.<ref>.supabase.co with
SUPABASE_DB_PASSWORD (or DATABASE_PASSWORD, or SUPABASE_SERVICE_ROLE_KEY as
the password — the exact fallback async_db.py uses in production).

Requires pg_dump/pg_restore on PATH (brew install libpq; the keg is not on
PATH by default — use: export PATH="$(brew --prefix)/opt/libpq/bin:$PATH").

The password is passed via the PGPASSWORD env var — never on the command
line, never printed.
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"


def _password() -> str:
    password = (
        os.getenv("SUPABASE_DB_PASSWORD")
        or os.getenv("DATABASE_PASSWORD")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    if not password:
        raise SystemExit("❌ No DB password source found (SUPABASE_DB_PASSWORD / DATABASE_PASSWORD / SUPABASE_SERVICE_ROLE_KEY)")
    return password


def _candidates(ref: str) -> list[str]:
    """Ordered connection candidates (host, user, port).

    Direct db.<ref>.supabase.co may not resolve on all networks (IPv6 /
    new platform); the pooler hosts (supavisor) use user postgres.<ref>.
    SUPABASE_POOLER_HOST, when set, is tried first.
    """
    pooler = os.getenv("SUPABASE_POOLER_HOST")
    cands: list[tuple[str, str, int]] = []
    if pooler:
        # Session pooler (5432) preferred for pg_dump; transaction pooler (6543)
        # as fallback — both use user postgres.<ref>.
        cands.append((pooler, f"postgres.{ref}", 5432))
        cands.append((pooler, f"postgres.{ref}", 6543))
    cands += [
        (f"db.{ref}.supabase.co", "postgres", 5432),
        (f"{ref}.supabase.co", "postgres", 5432),
        ("aws-1-ap-southeast-1.pooler.supabase.com", f"postgres.{ref}", 5432),
        ("aws-1-ap-southeast-1.pooler.supabase.com", f"postgres.{ref}", 6543),
        ("aws-0-ap-south-1.pooler.supabase.com", f"postgres.{ref}", 6543),
        ("aws-0-us-east-1.pooler.supabase.com", f"postgres.{ref}", 6543),
        (f"{ref}.pooler.supabase.com", f"postgres.{ref}", 6543),
    ]
    return [f"postgresql://{user}@{host}:{port}/postgres?sslmode=require" for host, user, port in cands]


def _probe(conn: str, password: str) -> bool:
    """True if the host accepts a connection (SELECT 1)."""
    env = {**os.environ, "PGPASSWORD": password}
    try:
        r = subprocess.run(
            ["psql", conn, "-tAc", "SELECT 1", "-o", "/dev/null"],
            env=env, capture_output=True, text=True, timeout=12,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def discover_conn() -> tuple[str, str]:
    """Return the first working (conn_string, password)."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    match = re.match(r"https?://([^.]+)\.supabase\.co", supabase_url)
    if not match:
        raise SystemExit("❌ Could not parse project ref from SUPABASE_URL")
    ref = match.group(1)
    password = _password()

    for conn in _candidates(ref):
        print(f"  probing {conn.split('@')[1].split('?')[0]}")
        if _probe(conn, password):
            print(f"  ✅ reachable")
            return conn, password
    raise SystemExit("❌ No reachable host — check network / SUPABASE_POOLER_HOST / Dashboard connection string")


def run(cmd: list[str], password: str) -> None:
    """Run a command with PGPASSWORD set; never print the secret."""
    env = {**os.environ, "PGPASSWORD": password}
    print("  $ " + " ".join(cmd[:2]) + " …")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-3000:])
        raise SystemExit(f"❌ Command failed: {cmd[0]}")
    if result.stdout.strip():
        print(result.stdout.strip()[-2000:])


def main() -> None:
    load_dotenv(ROOT / ".env")
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    conn, password = discover_conn()
    full_path = BACKUP_DIR / f"rhodey-full-{ts}.dump"
    public_path = BACKUP_DIR / f"rhodey-public-{ts}.sql"

    print(f"\n📦 Full logical dump (custom format) → {full_path}")
    run(["pg_dump", "-Fc", "--no-owner", "-f", str(full_path), conn], password)

    print(f"📦 Public-schema dump (plain SQL) → {public_path}")
    run(["pg_dump", "--no-owner", "-n", "public", "-f", str(public_path), conn], password)

    print("\n✅ Backup complete:")
    for p in (full_path, public_path):
        size = p.stat().st_size
        print(f"   {p.name}  ({size / 1e6:.1f} MB)")

    print("\n🔎 Verifying custom dump is readable…")
    run(["pg_restore", "-l", str(full_path), "|", "head", "-5"], password) if False else None
    verify = subprocess.run(
        ["bash", "-c", f"pg_restore -l '{full_path}' | head -5"],
        capture_output=True, text=True,
    )
    if verify.returncode == 0 and verify.stdout.strip():
        print(verify.stdout.strip())
        print("✅ Dump verified — archive is readable.")
    else:
        print(verify.stderr[-1000:])
        raise SystemExit("❌ Verification failed — archive unreadable")


if __name__ == "__main__":
    main()
