"""Shared copy-DB helpers for the tests/tenants isolation suite (plan §9).

DB-level tests run against a COPY of the production database — default is
the local restore (rhodey_restore_test), override with TENANTS_DSN. Tests
that need the DB skip when it is unreachable; the unit-level tests always run.

Marker rows use the TENANT_TEST_ prefix so leftover rows (from a crashed
run) are trivially identifiable and cleanable.
"""

import os
import subprocess

DSN = os.environ.get(
    "TENANTS_DSN",
    "postgresql://postgres@localhost:5433/rhodey_restore_test",
)

MARK = "TENANT_TEST_"


def psql(sql: str) -> str:
    """Run a SQL statement against the copy DB; return stdout (stripped)."""
    env = dict(os.environ)
    env["PATH"] = (
        "/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:"
        + env.get("PATH", "")
    )
    out = subprocess.run(
        ["psql", DSN, "-qtAc", sql], capture_output=True, text=True, env=env, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:400]}")
    return out.stdout.strip()


def db_available() -> bool:
    try:
        psql("select 1")
        return True
    except Exception:
        return False
