"""Migration chain replay — plans/75 Phase-3 #1 (ops surface, no aspect).

Proves the db/01..db/101 migration chain applies cleanly IN ORDER to a FRESH
schema, on a throwaway local Postgres cluster (initdb + pg_ctl). This is the
class of breakage that burned the project repeatedly — the replay already
caught a real one: db/101 dropped retrieval_triples while db/04's
retrieval_edges still FK'd to it (prod was only unaffected because the FK
never existed there; a fresh bootstrap failed).

Ops surface (like the rate limiter): no primary aspect — covered by the
nightly tier, exempt from the aspect-marker lint by design (plan §3).

Run (needs postgres binaries — initdb/pg_ctl on PATH or
/usr/lib/postgresql/*/bin, as on ubuntu CI runners):
    python scripts/replay_migrations.py          # the engine itself
    pytest tests/test_migrations_replay.py -q    # the wrapper
Skips cleanly when the binaries are unavailable.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Ops surface — exempt from the aspect-marker lint (see check_marker_presence.py).


def _find_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for pgdir in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (pgdir / name).exists():
            return str(pgdir / name)
    return None


def _pg_sharedir() -> str | None:
    """Postgres sharedir (extension control files live there), if resolvable."""
    pg_config = _find_bin("pg_config")
    if not pg_config:
        return None
    try:
        out = subprocess.run(
            [pg_config, "--sharedir"], capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _vector_extension_available() -> bool:
    """True when the pgvector 'vector.control' file exists on this system.

    The migration chain does CREATE EXTENSION vector (pgvector) at db/01, so
    replaying requires the extension installed at the OS level (apt
    postgresql-<ver>-pgvector). Checking for the control file — instead of
    just the postgres binaries — is what keeps this test a clean SKIP on
    runners that have initdb/pg_ctl but not pgvector (the original CI
    failure: "extension \"vector\" is not available").
    """
    sharedir = _pg_sharedir()
    if not sharedir:
        return False
    return (Path(sharedir) / "extension" / "vector.control").exists()


pytestmark = pytest.mark.skipif(
    not (_find_bin("initdb") and _find_bin("pg_ctl") and _vector_extension_available()),
    reason=(
        "postgres binaries (initdb/pg_ctl) or the pgvector extension unavailable "
        "— replay skipped (install postgresql-<ver>-pgvector to run)"
    ),
)


def test_migration_chain_replays_clean():
    """The whole db/ chain applies in numeric order to a fresh schema.

    Fails with the first structural break and its error; data migrations
    that reference pre-chain rows (db/06) are reported, not failed — they
    are environment-specific by design.
    """
    proc = subprocess.run(
        [sys.executable, "scripts/replay_migrations.py", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        # Surface the failure readably (the engine prints the failing
        # migration + error before the JSON summary).
        tail = (proc.stdout or proc.stderr or "")[-3000:]
        pytest.fail(f"migration chain replay FAILED (exit {proc.returncode}):\n{tail}")
    import json
    report = json.loads(proc.stdout)
    data_skips = [r for r in report["results"] if r.get("kind") == "data"]
    assert report["exit_code"] == 0
    assert report["applied"] == report["total"], \
        f"applied {report['applied']}/{report['total']} — see replay output"
    if data_skips:
        print(f"(expected data skips on empty base: {[r['file'] for r in data_skips]})")
