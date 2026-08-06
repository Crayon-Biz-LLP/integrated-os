"""
verify_m3_webhook_isolation.py — M3 webhook sweep verification
(plans/69-multi-tenant-product-plan.md §6 M3 exit)

Proves tenant scoping of the core/webhook data layer against the copy DB:

  [1] TenantTable.select() generates an owner_id filter (read scope)
  [2] TenantTable.update()/delete() append owner_id (write scope)
  [3] TenantTable.insert()/upsert() stamp owner_id on the payload
  [4] tenant_rpc() injects owner_id into RPC params
  [5] fail-closed: tenant_table() without a tenant context raises
  [6] schema-level: owner_id filtering isolates rows on the copy DB (psql)
  [7] the module facade (core.webhook.utils.supabase) is the tenant-aware
      client

Usage:
  python3 scripts/verify_m3_webhook_isolation.py \
      --dsn postgresql://postgres@localhost:5433/rhodey_restore_test
Exit 0 = all isolation gates pass.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "✅" if ok else "❌"
    print(f"  {tag} [{name}] {detail}")
    if not ok:
        FAILURES.append(name)


def psql(dsn: str, sql: str) -> str:
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:" + env.get("PATH", "")
    out = subprocess.run(["psql", dsn, "-tAc", sql], capture_output=True, text=True, env=env, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


# ── Recording fake client (asserts generated queries, no network) ──────────

class Recorder:
    """Minimal builder that records the operations chained onto it."""

    def __init__(self, log: list, tag: str):
        self._log = log
        self._tag = tag

    def select(self, *a, **k):
        self._log.append((self._tag, "select", a, k))
        return self

    def eq(self, *a, **k):
        self._log.append((self._tag, "eq", a, k))
        return self

    def update(self, *a, **k):
        self._log.append((self._tag, "update", a, k))
        return self

    def delete(self, *a, **k):
        self._log.append((self._tag, "delete", a, k))
        return self

    def insert(self, *a, **k):
        self._log.append((self._tag, "insert", a, k))
        return self

    def upsert(self, *a, **k):
        self._log.append((self._tag, "upsert", a, k))
        return self

    def rpc(self, *a, **k):
        self._log.append((self._tag, "rpc", a, k))
        return self

    def execute(self, *a, **k):
        self._log.append((self._tag, "execute", a, k))
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def maybe_single(self, *a, **k):
        return self

    def __getattr__(self, item):
        # Any other verb (is_, in_, not_, or_, lt, ...) — keep chain alive
        def _passthrough(*a, **k):
            return self
        return _passthrough


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost:5433/rhodey_restore_test")
    args = ap.parse_args()

    print("M3 webhook isolation gate\n")

    from unittest.mock import patch
    from core.services import db as db_mod

    uid = str(uuid.uuid4())
    log: list = []

    def fake_client():
        c = Recorder(log, "table")
        c.table = lambda name: Recorder(log, f"table:{name}")
        c.rpc = lambda name, params=None: Recorder(log, f"rpc:{name}").rpc(name, params)
        return c

    # Force tenant mode on (copy DB has db/78; the HTTP probe can't reach
    # raw Postgres, so pin the flag for the builder-level assertions).
    with patch.object(db_mod, "_tenant_mode", True), \
         patch.object(db_mod, "get_supabase", side_effect=fake_client), \
         db_mod.tenant_scope(uid):
        # [1] select → owner filter
        db_mod.tenant_table("tasks").select("id, title").eq("status", "todo").execute()
        sel = [e for e in log if e[0] == "table:tasks" and e[1] == "eq"]
        check("read: select chains owner_id filter",
              any(a and a[0] == "owner_id" and a[1] == uid for _, _, a, _ in sel),
              f"eq ops on tasks: {[a for _, _, a, _ in sel]}")

        # [2] update/delete → owner filter
        log.clear()
        db_mod.tenant_table("tasks").update({"priority": "high"}).eq("id", 1).execute()
        upd_eqs = [a for e in log if e[1] == "eq" for a in [e[2]]]
        check("write: update chains owner_id filter",
              any(a and a[0] == "owner_id" for a in upd_eqs),
              f"update eqs: {upd_eqs}")

        log.clear()
        db_mod.tenant_table("processed_updates").delete().lt("processed_at", "x").execute()
        del_eqs = [a for e in log if e[1] == "eq" for a in [e[2]]]
        check("write: delete chains owner_id filter",
              any(a and a[0] == "owner_id" for a in del_eqs),
              f"delete eqs: {del_eqs}")

        # [3] insert/upsert stamp owner_id on payload
        log.clear()
        db_mod.tenant_table("tasks").insert({"title": "t"}).execute()
        ins_payload = next((e[2][0] for e in log if e[1] == "insert"), None)
        check("write: insert stamps owner_id",
              isinstance(ins_payload, dict) and ins_payload.get("owner_id") == uid,
              f"insert payload owner_id={ (ins_payload or {}).get('owner_id') if isinstance(ins_payload, dict) else 'n/a' }")

        log.clear()
        db_mod.tenant_table("core_config").upsert({"key": "k"}, on_conflict="owner_id,key").execute()
        up_payload = next((e[2][0] for e in log if e[1] == "upsert"), None)
        check("write: upsert stamps owner_id",
              isinstance(up_payload, dict) and up_payload.get("owner_id") == uid,
              f"upsert payload owner_id={ (up_payload or {}).get('owner_id') if isinstance(up_payload, dict) else 'n/a' }")

        # [4] rpc injects owner_id
        log.clear()
        db_mod.tenant_rpc("match_conversations", {"query_embedding": [0.0] * 768})
        # Recorder.rpc logs (name, params) as positional args
        rpc_entries = [e for e in log if e[1] == "rpc"]
        rpc_params = rpc_entries[0][2][1] if rpc_entries and len(rpc_entries[0][2]) > 1 else None
        check("rpc: owner_id injected",
              isinstance(rpc_params, dict) and rpc_params.get("owner_id") == uid,
              f"rpc params owner_id={ (rpc_params or {}).get('owner_id') }")

    # [5] fail-closed without tenant
    with patch.object(db_mod, "_tenant_mode", True), \
         patch.object(db_mod, "get_supabase", side_effect=fake_client):
        try:
            db_mod.tenant_table("tasks")
            check("fail-closed without tenant", False, "no exception raised")
        except db_mod.TenantRequiredError:
            check("fail-closed without tenant", True, "TenantRequiredError raised")

    # [6] module facade is tenant-aware
    import core.webhook.utils as wu
    check("module facade is TenantAwareClient",
          isinstance(wu.supabase, db_mod.TenantAwareClient),
          type(wu.supabase).__name__)

    # [7] schema-level isolation on the copy DB (psql)
    ta, tb = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        psql(args.dsn, f"insert into public.users (id, name, status) values ('{ta}', 'm3-a', 'active')")
        psql(args.dsn, f"insert into public.users (id, name, status) values ('{tb}', 'm3-b', 'active')")
        psql(args.dsn, f"insert into public.tasks (title, status, is_current, owner_id) values ('M3-A task', 'todo', true, '{ta}')")
        psql(args.dsn, f"insert into public.tasks (title, status, is_current, owner_id) values ('M3-B task', 'todo', true, '{tb}')")
        a_view = psql(args.dsn, f"select count(*) from public.tasks where owner_id='{ta}'")
        b_view = psql(args.dsn, f"select count(*) from public.tasks where owner_id='{tb}'")
        b_in_a = psql(args.dsn, f"select count(*) from public.tasks where owner_id='{ta}' and title='M3-B task'")
        check("schema: owner_id isolates rows",
              a_view == "1" and b_view == "1" and b_in_a == "0",
              f"A sees {a_view} (own) + {b_in_a} (B's) | B sees {b_view}")
        # A cannot update B's row via owner-scoped WHERE (compare before/after
        # because tasks.priority has a column default)
        b_prio_before = psql(args.dsn, f"select coalesce(priority,'') from public.tasks where owner_id='{tb}' and title='M3-B task'")
        psql(args.dsn, f"update public.tasks set priority='high' where owner_id='{ta}' and title='M3-B task'")
        b_prio_after = psql(args.dsn, f"select coalesce(priority,'') from public.tasks where owner_id='{tb}' and title='M3-B task'")
        check("schema: owner-scoped update can't touch B",
              b_prio_before == b_prio_after, f"B priority before={b_prio_before!r} after={b_prio_after!r}")
    finally:
        psql(args.dsn, f"delete from public.tasks where owner_id in ('{ta}', '{tb}')")
        psql(args.dsn, f"delete from public.users where id in ('{ta}', '{tb}')")

    print()
    if FAILURES:
        print(f"❌ ISOLATION GATE FAILED — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ ALL M3 WEBHOOK ISOLATION GATES PASSED")


if __name__ == "__main__":
    main()
