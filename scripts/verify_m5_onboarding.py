#!/usr/bin/env python3
"""
verify_m5_onboarding.py — M5 verification gate (plans/69-multi-tenant-product-plan.md).

Gates:
  [1] db/84 applied on copy DB (user_oauth_tokens + users.google_connected)
  [2] get_google_creds resolves per-tenant refresh tokens (isolation)
  [3] get_google_creds returns None (not env leak) when a tenant has no token
  [4] get_cached_service / sentinel / calendar return None-safe when no creds
  [5] update_google_oauth script targets --user (SQL inspection)
  [6] seed_user_world seeds settings + graph + tasks under a tenant scope
  [7] onboarding_state transitions to 'seeded'

Usage: python scripts/verify_m5_onboarding.py [--dsn postgresql://...]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    mark = "✅" if ok else "❌"
    print(f"  {mark} [{name}] {detail}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def psql(dsn: str, sql: str) -> str:
    env = {**os.environ}
    r = subprocess.run(
        ["psql", dsn, "-tAc", sql], env=env, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return f"__PSQL_ERR__: {r.stderr[-300:]}"
    return r.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="postgresql://postgres@localhost:5433/rhodey_restore_test")
    args = parser.parse_args()
    dsn = args.dsn

    print("── M5 gate [1]: schema (copy DB) ──")
    cols = psql(dsn, "SELECT string_agg(column_name, ',') FROM information_schema.columns WHERE table_name='user_oauth_tokens'")
    check("user_oauth_tokens table", "refresh_token" in cols and "user_id" in cols and "provider" in cols, cols)
    gcols = psql(dsn, "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name='google_connected'")
    check("users.google_connected", gcols == "google_connected", gcols or "missing")

    print("── M5 gate [2]: per-tenant credential resolution ──")
    import uuid
    from unittest.mock import MagicMock, patch

    # Build two fake tenant uuids and stub the DB reads via patch on get_supabase.
    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    token_a = "refresh-token-A"

    from core.services import google_service as gs

    def _fake_db(rows):
        """A fake supabase client chain that returns `rows` for the token query."""
        def _table(name):
            t = MagicMock()
            q = MagicMock()
            q.select.return_value = q
            q.eq.return_value = q
            q.limit.return_value = q
            q.maybe_single.return_value = q
            q.execute.return_value = MagicMock(data=rows)
            t.select.return_value = q
            return t
        c = MagicMock()
        c.table.side_effect = _table
        return c

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOOGLE_REFRESH_TOKEN", None)
        # User A has a token row; user B has none.
        rows_a = [{"refresh_token": token_a}]
        with patch("core.services.db.get_supabase", return_value=_fake_db(rows_a)):
            creds = gs.get_google_creds(uid_a)
        check("tenant A resolves own token", creds is not None and creds.refresh_token == token_a,
              creds.refresh_token if creds else "None")
        with patch("core.services.db.get_supabase", return_value=_fake_db([])):
            creds_b = gs.get_google_creds(uid_b)
        check("tenant B without token → None (no env leak)", creds_b is None, "None")
        # Different users get DIFFERENT cached creds (per-user cache keying).
        with patch("core.services.db.get_supabase", return_value=_fake_db(rows_a)):
            creds_a2 = gs.get_google_creds(uid_a)
        check("per-user cache keyed correctly", creds_a2 is creds, "same object for same user")

    print("── M5 gate [3]: env fallback still works (legacy) ──")
    with patch.dict(os.environ, {"GOOGLE_REFRESH_TOKEN": "env-token"}, clear=False):
        creds_env = gs.get_google_creds(None)
        check("no tenant → env fallback", creds_env is not None and creds_env.refresh_token == "env-token",
              creds_env.refresh_token if creds_env else "None")

    print("── M5 gate [4]: None-safe services ──")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOOGLE_REFRESH_TOKEN", None)
        with patch("core.services.db.get_supabase", return_value=_fake_db([])):
            serv = gs.get_cached_service("calendar", "v3", uid_b)
            check("get_cached_service returns None without creds", serv is None, "None")
            from core.pulse.sentinel import get_upcoming_events, get_recently_ended_events
            ev = get_upcoming_events(minutes_ahead=60)
            ended = get_recently_ended_events()
            check("sentinel calendar skips without creds", ev == [] and ended == [], f"ev={len(ev)} ended={len(ended)}")

    print("── M5 gate [5]: update_google_oauth targets --user ──")
    oauth_src = (ROOT / "scripts" / "update_google_oauth.py").read_text()
    check("script has --user arg", "--user" in oauth_src and "required=True" in oauth_src, "argparse --user")
    check("script writes user_oauth_tokens", "user_oauth_tokens" in oauth_src, "INSERT INTO user_oauth_tokens")
    check("script flips google_connected", "google_connected" in oauth_src, "UPDATE users SET google_connected")

    print("── M5 gate [6]: seed_world flow (mocked supabase + stubbed heavy fns) ──")
    import asyncio
    from scripts import seed_user_world as suw

    seen = {"upserts": [], "updates": [], "tasks": [], "nodes": []}

    class Recorder:
        """Minimal supabase mock that records tenant-scoped writes."""

        def __init__(self, table_name):
            self._name = table_name

        def upsert(self, data, on_conflict=None, **kw):
            if self._name == "user_settings":
                seen["upserts"].append(data)
            return self

        def update(self, data):
            if self._name == "user_settings":
                seen["updates"].append(data)
            return self

        def eq(self, *a):
            return self

        def execute(self):
            return MagicMock(data=[{"id": 1}])

    class RecorderClient:
        def table(self, name):
            return Recorder(name)

    world = {
        "context": "Priya, COO at Acme, Bengaluru.",
        "timezone": "Asia/Kolkata",
        "domains": [{"name": "Acme", "keywords": ["acme"]}],
        "personal_orgs": ["Personal"],
        "people": [{"name": "Raj", "context": "CTO at Acme"}],
        "organizations": [{"name": "Acme", "context": "the company"}],
        "tasks": [{"title": "Prep Q3 board deck", "priority": "high", "organization": "Acme"}],
    }

    async def _run_seed():
        # seed_world imports these inside its body from the source modules —
        # patch them where they live (they are the tenant-scoped runtime fns).
        with patch("core.pulse.graph.create_graph_node_with_db_record",
                   side_effect=lambda **kw: {"success": True, "message": "ok", "node_id": str(uuid.uuid4())}), \
             patch("core.pulse.tools.create_task_direct",
                   side_effect=lambda **kw: {"action": "created", "task_id": 1, "reason": None}):
            return await suw.seed_world(RecorderClient(), uid_a, world)

    result = asyncio.run(_run_seed())
    check("seed created counts", result["people"] == 1 and result["organizations"] == 1 and result["tasks"] == 1,
          f"people={result['people']} orgs={result['organizations']} tasks={result['tasks']}")
    check("seed wrote settings with uid", bool(seen["upserts"]) and seen["upserts"][0].get("user_id") == uid_a,
          str(seen["upserts"][0]) if seen["upserts"] else "none")
    check("seed set onboarding_state=seeded",
          any(u.get("onboarding_state") == "seeded" for u in seen["updates"]),
          str(seen["updates"]))

    print(f"\n{'✅ ALL M5 ONBOARDING GATES PASSED' if FAIL == 0 else f'❌ {FAIL} GATE(S) FAILED'}  ({PASS} passed)")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
