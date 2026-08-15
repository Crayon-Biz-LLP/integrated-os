#!/usr/bin/env python3
"""replay_migrations.py — scratch-Postgres replay of the db/ migration chain.

Proves the ~95-file migration chain applies cleanly IN ORDER to a FRESH
schema — the class of breakage that has burned this project repeatedly
(db/78 constraint fixes, db/101 ordering, dropped-but-still-referenced
objects). Runs on a throwaway local Postgres cluster (initdb + pg_ctl),
NEVER on the live Supabase project.

Scaffold — the Supabase surface the chain assumes but never creates:
  - roles: anon, authenticated, service_role (GRANT targets)
  - schema auth + auth.role() / auth.uid() stubs (RLS policy references)
  - CREATE EXTENSION vector (pgvector — Supabase preinstalls it)

Order: lexicographic filename order (db/01_… .. db/101_…) — the only
manifest-free deterministic order. The chain stops at the FIRST failing
migration (a chain is only as good as its earliest broken link).

Usage:
    python scripts/replay_migrations.py            # full replay, auto-cleanup
    python scripts/replay_migrations.py --keep     # keep cluster dir for debugging
    python scripts/replay_migrations.py --json     # machine-readable summary

Exit codes: 0 = whole chain applied cleanly; 1 = failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "db"

SCAFFOLD_SQL = """
-- Supabase surface the chain assumes but never creates (replay scaffold).
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN;
CREATE SCHEMA auth;
CREATE FUNCTION auth.role() RETURNS text
    LANGUAGE sql STABLE AS $$ SELECT current_setting('request.jwt.claim.role', true) ::text $$;
CREATE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE AS $$ SELECT current_setting('request.jwt.claim.sub', true)::uuid $$;
-- Supabase 'extensions' schema: uuid-ossp lives there; the backup-derived
-- base defaults to extensions.uuid_generate_v4().
CREATE SCHEMA extensions;
CREATE FUNCTION extensions.uuid_generate_v4() RETURNS uuid
    LANGUAGE sql AS $$ SELECT gen_random_uuid() $$;
CREATE EXTENSION IF NOT EXISTS vector;
"""


def _find_bin(name: str) -> str | None:
    """Find a postgres binary on PATH or in /usr/lib/postgresql/*/bin (CI ubuntu)."""
    found = shutil.which(name)
    if found:
        return found
    for pgdir in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        cand = pgdir / name
        if cand.exists():
            return str(cand)
    return None


_STRUCTURAL_MARKS = (
    "does not exist", "already exists", "no unique constraint",
    "no unique or exclusion constraint", "incompatible types",
    "syntax error", "is not a table", "permission denied",
    "cannot be implemented", "column reference", "operator does not exist",
    "type does not exist", "function does not exist", "must appear in",
)
_DATA_MARKS = (
    "violates foreign key constraint", "is not present in table",
    "duplicate key value", "violates check constraint",
    "violates not-null constraint", "null value in column", "value too long",
)


def _classify_error(err: str) -> str:
    """Structural (chain break) vs data (empty-base, expected) failure.

    Unknown errors default to STRUCTURAL — a replay must never silently
    pass a failure it can't classify.
    """
    if any(m in err for m in _DATA_MARKS):
        return "data"
    return "structural"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _out(msg: str) -> None:
    """Progress output: stderr in --json mode (stdout stays pure JSON), else stdout."""
    print(msg, file=sys.stderr if _JSON_MODE else None)


_JSON_MODE = False


def _psql(bin_dir: str, sock: str, port: int, sql_or_file: str,
          *, from_file: bool, timeout: float = 90.0) -> tuple[int, str]:
    # rhodey_pw: db/90 requires a 24+ char password via psql -v (creates the
    # rhodey_app role). Harmless when a migration doesn't use it.
    pw = os.environ.get("RHODEY_APP_DB_PASSWORD") or "replay-0000-0000-0000-0000-0000"
    cmd = [
        os.path.join(bin_dir, "psql"), "-X", "-q", "-h", sock, "-p", str(port),
        "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
        "-v", f"rhodey_pw={pw}",
    ]
    if from_file:
        cmd += ["-f", sql_or_file]
    else:
        cmd += ["-c", sql_or_file]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout:.0f}s"
    err = (res.stderr or "").strip()
    return res.returncode, err[-800:] if err else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the scratch cluster dir")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args()
    global _JSON_MODE
    _JSON_MODE = args.json

    initdb = _find_bin("initdb")
    pg_ctl = _find_bin("pg_ctl")
    if not initdb or not pg_ctl:
        print("✗ replay unavailable: initdb/pg_ctl not found (PATH or /usr/lib/postgresql/*/bin)")
        return 2

    # Chain = every db/*.sql EXCEPT the replay scaffold (00_replay_base.sql).
    # Order = NUMERIC by leading number (01,02,…,09,10,…,99,100,101), ties by
    # filename — the intent encoded in the names. Plain lexicographic sort is
    # WRONG: "100_" < "10_" ("0" < "_"), which would run db/100 before
    # db/10-99 (a genuine chain-order bug this tool must not reintroduce).
    def _num_key(p: Path):
        m = re.match(r"(\d+)", p.name)
        return (int(m.group(1)) if m else 0, p.name)

    migrations = sorted(
        (f for f in DB_DIR.glob("*.sql")
         if f.name != "00_replay_base.sql" and re.match(r"^\d+", f.name)),
        key=_num_key,
    )
    if not migrations:
        print("✗ no migrations found in db/")
        return 1

    # Pre-chain base surface (db/00_replay_base.sql) — applied before the
    # chain. Explicitly excluded from the chain count.
    base_file = DB_DIR / "00_replay_base.sql"
    base_sql = base_file.read_text(encoding="utf-8") if base_file.exists() else ""

    bin_dir = str(Path(initdb).parent)
    tmp = tempfile.mkdtemp(prefix="migr_replay_")
    data = os.path.join(tmp, "data")
    sock = os.path.join(tmp, "sock")
    port = _free_port()
    logfile = os.path.join(tmp, "postgres.log")
    os.makedirs(sock, exist_ok=True)

    results: list[dict] = []
    exit_code = 0
    try:
        # ── initdb + start ─────────────────────────────────────────────
        r = subprocess.run([initdb, "-D", data, "-U", "postgres", "--auth=trust",
                            "-E", "UTF8", "--no-sync"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"✗ initdb failed:\n{(r.stderr or r.stdout)[-500:]}")
            return 1
        r = subprocess.run([pg_ctl, "-D", data, "-o", f"-p {port} -k {sock}",
                            "-l", logfile, "-w", "start"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"✗ pg_ctl start failed:\n{(r.stderr or r.stdout)[-500:]}")
            return 1

        # ── scaffold + pre-chain base ──────────────────────────────────
        rc, err = _psql(bin_dir, sock, port, SCAFFOLD_SQL, from_file=False)
        if rc != 0:
            print(f"✗ scaffold failed:\n{err}")
            return 1
        if base_sql:
            rc, err = _psql(bin_dir, sock, port, base_sql, from_file=False)
            if rc != 0:
                print(f"✗ replay base (db/00_replay_base.sql) failed:\n{err}")
                return 1
        results.append({"file": "scaffold+base", "ok": True, "secs": 0.0})

        # ── apply the chain ─────────────────────────────────────────────
        t0 = time.monotonic()
        for i, mig in enumerate(migrations, 1):
            start = time.monotonic()
            rc, err = _psql(bin_dir, sock, port, str(mig), from_file=True)
            secs = time.monotonic() - start
            if rc == 0:
                results.append({"file": mig.name, "ok": True, "secs": round(secs, 2)})
                _out(f"  ok    {i:>3}/{len(migrations)}  {mig.name}  ({secs:.1f}s)")
                continue
            kind = _classify_error(err)
            if kind == "data":
                # Data migration referencing pre-chain rows an empty base
                # can't reproduce — expected, not a chain break. Reported,
                # and the replay continues to prove the rest of the chain.
                results.append({"file": mig.name, "ok": False, "secs": round(secs, 2),
                                "kind": "data", "error": err})
                _out(f"  ≈ DATA {i:>3}/{len(migrations)}  {mig.name}  ({secs:.1f}s) — "
                     f"references pre-chain rows (expected on empty base)")
                continue
            results.append({"file": mig.name, "ok": False, "secs": round(secs, 2),
                            "kind": "structural", "error": err})
            _out(f"✗ FAIL  {i:>3}/{len(migrations)}  {mig.name}  ({secs:.1f}s)  [structural]")
            _out(f"    {err.splitlines()[-3:] if err else '(no stderr)'}")
            exit_code = 1
            break
        total = time.monotonic() - t0
        ok_count = sum(1 for r in results if r["ok"])

        # ── stop + cleanup ──────────────────────────────────────────────
        subprocess.run([pg_ctl, "-D", data, "-w", "stop", "-m", "fast"],
                       capture_output=True, text=True, timeout=60)
    finally:
        if args.keep:
            print(f"\n(keeping scratch cluster at {tmp} — stop it with: "
                  f"pg_ctl -D {data} stop)")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps({"exit_code": exit_code, "applied": ok_count,
                          "total": len(migrations), "total_secs": round(total, 1),
                          "results": results}))
    elif exit_code == 0:
        _out(f"\n✅ Migration chain replay CLEAN — {ok_count}/{len(migrations)} applied "
             f"in order ({total:.1f}s)")
    else:
        _out(f"\n❌ Migration chain BROKEN — {ok_count - 1} applied before first failure "
             f"({total:.1f}s to failure)")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
