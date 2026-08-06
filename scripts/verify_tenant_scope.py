#!/usr/bin/env python3
"""
verify_tenant_scope.py — M1 live smoke test against a local restore copy.

The local copy is plain Postgres (no PostgREST), so this simulates the exact
SQL semantics the tenant layer (core/services/db.py) produces:

  1. issue a per-user API key (SHA-256 hash stored in users.api_key_hash)
  2. resolve the key → user            (require_api_auth equivalent)
  3. tenant-scoped write with owner_id (owner_id NOT NULL must hold)
  4. tenant-scoped read isolated by owner
  5. write without owner_id must FAIL (owner_id NOT NULL enforced)
  6. cleanup (no [TENANT-TEST] residue)

Usage:
    python scripts/verify_tenant_scope.py --dsn postgresql://postgres@localhost:5433/rhodey_restore_test
"""

import argparse
import hashlib
import subprocess
import sys
import uuid


def _psql(dsn: str, sql: str) -> str:
    r = subprocess.run(["psql", dsn, "-tAc", sql], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"❌ psql failed:\n{r.stderr[-1500:]}")
    return r.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="M1 tenant-scope live verification")
    parser.add_argument("--dsn", required=True, help="Local copy DB (Postgres DSN)")
    args = parser.parse_args()

    key = f"verify-{uuid.uuid4().hex[:8]}"
    api_hash = hashlib.sha256(key.encode()).hexdigest()
    name = "[TENANT-TEST] M1-verify"
    checks: list[tuple[str, bool]] = []

    # 0. pre-clean any leftover from a failed earlier run (idempotent re-runs)
    _psql(args.dsn, "delete from public.users where name = '[TENANT-TEST] M1-verify'")
    _psql(args.dsn, "delete from public.core_config where key like '[TENANT-TEST]:M1%'")

    # 1. issue key (psql prints the returned id AND the 'INSERT 0 1' tag on
    #    separate lines — take only the first line for the uuid)
    uid = _psql(
        args.dsn,
        f"insert into public.users (name, api_key_hash) values ('{name}', '{api_hash}') returning id",
    ).splitlines()[0]
    checks.append(("per-user key issued (hash stored)", bool(uid)))

    try:
        # 2. resolve key → user
        resolved = _psql(args.dsn, f"select id from public.users where api_key_hash = '{api_hash}' limit 1")
        checks.append(("key resolves to the right user", resolved == uid))

        # 3. tenant-scoped write (owner_id set explicitly — as TenantTable.insert does)
        _psql(args.dsn, f"insert into public.core_config (owner_id, key, content) values ('{uid}', '[TENANT-TEST]:M1', '{{\"ok\": true}}'::jsonb)")
        checks.append(("tenant-scoped write with owner_id accepted", True))

        # 4. tenant-scoped read isolated by owner
        mine = _psql(args.dsn, f"select count(*) from public.core_config where owner_id = '{uid}' and key = '[TENANT-TEST]:M1'")
        others = _psql(args.dsn, f"select count(*) from public.core_config where owner_id <> '{uid}' and key = '[TENANT-TEST]:M1'")
        checks.append(("scoped read sees only own rows", mine == "1" and others == "0"))

        # 5. write without owner_id must be rejected (NOT NULL)
        try:
            _psql(args.dsn, "insert into public.core_config (key, content) values ('[TENANT-TEST]:M1-null', '{}'::jsonb)")
            checks.append(("owner_id NOT NULL enforced (unscoped write blocked)", False))
        except SystemExit:
            checks.append(("owner_id NOT NULL enforced (unscoped write blocked)", True))
    finally:
        # 6. cleanup
        _psql(args.dsn, "delete from public.core_config where key like '[TENANT-TEST]:M1%'")
        _psql(args.dsn, f"delete from public.users where id = '{uid}'")
        left = _psql(args.dsn, "select count(*) from public.users where name = '[TENANT-TEST] M1-verify'")
        checks.append(("cleanup complete (no [TENANT-TEST] residue)", left == "0"))

    print("\nM1 tenant-scope verification:")
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    sys.exit(0 if all(ok for _, ok in checks) else 1)


if __name__ == "__main__":
    main()
