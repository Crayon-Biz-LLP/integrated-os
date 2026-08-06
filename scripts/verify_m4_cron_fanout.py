"""
verify_m4_cron_fanout.py — M4 cron fan-out + per-tenant push verification
(plans/69-multi-tenant-product-plan.md §6 M4)

Gates:
  [1] active_user_ids() returns every active user (copy DB)
  [2] process_sentinel / process_decision_pulse fan out: with N active users
      the wrapper runs the impl once per user, each under its own tenant
      scope (spy on the impl + record get_tenant() inside)
  [2b] process_pulse (briefing) + run_full_health_check fan out per active
      user with the same wrapper pattern; legacy unscoped fallbacks
  [3] roundup fan-out shape: /api/roundup uses active_user_ids + tenant_scope
  [4] core_config_upsert picks 'owner_id,key' in tenant mode, 'key' legacy
  [5] resolve_telegram_chat_id: per-user value wins; single-active-user world
      falls back to env; multi-user without a value → None (no cross-tenant
      Telegram leak)
  [6] push_notification scoped_tokens_query carries owner filter when a
      tenant is set
  [7] audit_logger stamps owner_id from the tenant context

Usage:
  python3 scripts/verify_m4_cron_fanout.py \
      --dsn postgresql://postgres@localhost:5433/rhodey_restore_test
Exit 0 = all M4 gates pass.
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
    out = subprocess.run(["psql", dsn, "-qtAc", sql], capture_output=True, text=True, env=env, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


class Recorder:
    """Minimal builder that records operations chained onto it."""

    def __init__(self, log: list, tag: str, result: dict | None = None):
        self._log = log
        self._tag = tag
        self._result = result

    def select(self, *a, **k):
        self._log.append((self._tag, "select", a, k))
        return self

    def eq(self, *a, **k):
        self._log.append((self._tag, "eq", a, k))
        return self

    def execute(self, *a, **k):
        self._log.append((self._tag, "execute", a, k))
        class _Res:
            def __init__(self, data):
                self.data = data
        return _Res(self._result)

    def upsert(self, data, on_conflict=None, **k):
        self._log.append((self._tag, "upsert", (data,), {**k, "on_conflict": on_conflict}))
        class _Res:
            def __init__(self, data):
                self.data = data
        return _Res(self._result)

    def __getattr__(self, item):
        def _passthrough(*a, **k):
            return self
        return _passthrough


class FakeClient:
    """Supabase-client stand-in: .table(name) returns a chainable Recorder
    whose execute() returns a canned dict (or list)."""

    def __init__(self, log: list, table_result: dict | list | None = None):
        self._log = log
        self._table_result = table_result

    def table(self, name):
        if isinstance(self._table_result, dict) and name in self._table_result:
            return Recorder(self._log, f"table:{name}", result=self._table_result[name])
        return Recorder(self._log, f"table:{name}", result=self._table_result)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost:5433/rhodey_restore_test")
    args = ap.parse_args()

    print("M4 cron fan-out + per-tenant push gate\n")

    from unittest.mock import patch
    from core.services import db as db_mod

    log: list = []

    # [1] active users on the copy DB (expect >=1; Danny's row exists)
    db_active = psql(args.dsn, "select count(*) from public.users where status='active'")
    check("copy DB has >=1 active user", int(db_active or 0) >= 1,
          f"active={db_active}")
    db_uid = psql(args.dsn, "select id from public.users where status='active' order by created_at limit 1")
    first_uid = db_uid or str(uuid.uuid4())

    # Also verify active_user_ids() itself under a mock client (deterministic)
    fake_uid_a, fake_uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    with patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(
        log, {"users": [{"id": fake_uid_a}, {"id": fake_uid_b}]})):
        mock_uids = db_mod.active_user_ids()
    check("active_user_ids parses mock users", mock_uids == [fake_uid_a, fake_uid_b],
          f"got={mock_uids}")

    # [2] sentinel / decision_pulse fan out per tenant
    import core.pulse.sentinel as sentinel_mod
    import core.pulse.decision_pulse as dp_mod

    seen_tenants: list = []

    async def _fake_impl(auth_secret=None, trigger=None):
        seen_tenants.append(db_mod.get_tenant())
        return {"success": True}

    import asyncio

    with patch.object(sentinel_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(sentinel_mod, "_process_sentinel_impl", side_effect=_fake_impl):
        seen_tenants.clear()
        asyncio.run(sentinel_mod.process_sentinel("secret", trigger="test"))
    check("process_sentinel fans out once per active user (tenant set each run)",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")

    with patch.object(dp_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(dp_mod, "_process_decision_pulse_impl", side_effect=_fake_impl):
        seen_tenants.clear()
        asyncio.run(dp_mod.process_decision_pulse("secret", trigger="test"))
    check("process_decision_pulse fans out once per active user (tenant set each run)",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")

    # Legacy fallback: no active users → impl runs once unscoped
    with patch.object(sentinel_mod, "active_user_ids", return_value=[]), \
         patch.object(sentinel_mod, "_process_sentinel_impl", side_effect=_fake_impl):
        seen_tenants.clear()
        asyncio.run(sentinel_mod.process_sentinel("secret", trigger="test"))
    check("legacy: no active users → single unscoped run",
          len(seen_tenants) == 1 and seen_tenants[0] is None,
          f"runs={len(seen_tenants)} tenants={seen_tenants}")

    # [2b] briefing + health check fan out per tenant (M6 — closes the last
    # cron fan-out gap; previously single-channel tenant only)
    import core.pulse.briefing as briefing_mod
    import core.pulse.pipeline as pipeline_mod

    async def _fake_pulse_impl(auth_secret=None, request_id=None, trigger=None):
        seen_tenants.append(db_mod.get_tenant())
        return {"success": True, "briefing": f"briefing {db_mod.get_tenant()}"}

    async def _fake_health_impl():
        seen_tenants.append(db_mod.get_tenant())
        return {"issues": [], "report": "ok", "counts": {"checks": 1}}

    with patch.object(briefing_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(briefing_mod, "_process_pulse_impl", side_effect=_fake_pulse_impl):
        seen_tenants.clear()
        pres = asyncio.run(briefing_mod.process_pulse(trigger="test"))
    check("process_pulse (briefing) fans out once per active user (tenant set each run)",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")
    check("process_pulse keeps legacy response shape + per-tenant results",
          pres.get("briefing") is not None and pres.get("tenants") == 2
          and len(pres.get("results", [])) == 2,
          f"keys={sorted(pres.keys())}")

    with patch.object(pipeline_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(pipeline_mod, "_run_full_health_check_impl", side_effect=_fake_health_impl):
        seen_tenants.clear()
        hres = asyncio.run(pipeline_mod.run_full_health_check())
    check("run_full_health_check fans out once per active user (tenant set each run)",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")
    check("health check keeps issues/report/counts + tenant count",
          isinstance(hres.get("issues"), list) and "ok" in hres.get("report", "")
          and hres.get("tenants") == 2,
          f"keys={sorted(hres.keys())}")

    # Failure isolation: tenant A raises → tenant B still runs, and the
    # aggregate keeps the legacy shape (no KeyError on report/briefing).
    def _boom_pulse_impl(*a, **k):
        seen_tenants.append(db_mod.get_tenant())
        raise RuntimeError("tenant down")

    def _ok_health_impl():
        seen_tenants.append(db_mod.get_tenant())
        return {"issues": [], "report": "ok", "counts": {"checks": 1}}

    def _boom_health_impl():
        seen_tenants.append(db_mod.get_tenant())
        raise RuntimeError("tenant down")

    with patch.object(briefing_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(briefing_mod, "_process_pulse_impl", side_effect=_boom_pulse_impl):
        seen_tenants.clear()
        fpres = asyncio.run(briefing_mod.process_pulse(trigger="test"))
    check("failure isolation: one tenant's briefing failure doesn't abort the loop",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")
    check("failure isolation: aggregate still exposes briefing key (None-safe)",
          "briefing" in fpres and fpres.get("results") and all("error" in r for r in fpres["results"]),
          f"keys={sorted(fpres.keys())} results={fpres.get('results')}")

    with patch.object(briefing_mod, "active_user_ids", return_value=[]), \
         patch.object(briefing_mod, "_process_pulse_impl", side_effect=_fake_pulse_impl):
        seen_tenants.clear()
        asyncio.run(briefing_mod.process_pulse(trigger="test"))
    check("legacy: briefing with no active users → single unscoped run",
          len(seen_tenants) == 1 and seen_tenants[0] is None,
          f"runs={len(seen_tenants)} tenants={seen_tenants}")

    # Health-check failure isolation: tenant A raises → B still runs, and the
    # aggregate ALWAYS carries issues/report/counts (api/index.py:228 returns
    # the whole dict; check_pipeline_health reads result["report"] directly).
    with patch.object(pipeline_mod, "active_user_ids", return_value=[fake_uid_a, fake_uid_b]), \
         patch.object(pipeline_mod, "_run_full_health_check_impl", side_effect=_boom_health_impl):
        seen_tenants.clear()
        fhres = asyncio.run(pipeline_mod.run_full_health_check())
    check("failure isolation: one tenant's health failure doesn't abort the loop",
          len(seen_tenants) == 2 and seen_tenants == [fake_uid_a, fake_uid_b],
          f"runs={len(seen_tenants)} tenants={seen_tenants}")
    check("failure isolation: aggregate keeps issues/report/counts keys (no KeyError)",
          "issues" in fhres and "report" in fhres and "counts" in fhres
          and len(fhres.get("issues", [])) == 2,
          f"keys={sorted(fhres.keys())} issues={fhres.get('issues')}")

    with patch.object(pipeline_mod, "active_user_ids", return_value=[]), \
         patch.object(pipeline_mod, "_run_full_health_check_impl", side_effect=_fake_health_impl):
        seen_tenants.clear()
        asyncio.run(pipeline_mod.run_full_health_check())
    check("legacy: health check with no active users → single unscoped run",
          len(seen_tenants) == 1 and seen_tenants[0] is None,
          f"runs={len(seen_tenants)} tenants={seen_tenants}")

    # [3] roundup fans out (import-level: uses active_user_ids + tenant_scope)
    import inspect
    import api.index as api_mod
    src = inspect.getsource(api_mod.roundup_route)
    check("roundup_route iterates active users per tenant",
          "active_user_ids()" in src and "tenant_scope(uid)" in src,
          "fan-out loop present" if ("active_user_ids()" in src and "tenant_scope(uid)" in src) else "NOT fan-out")

    # [4] core_config_upsert conflict target by tenant mode
    with patch.object(db_mod, "tenant_mode_enabled", return_value=True), \
         patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(log)):
        log.clear()
        db_mod.core_config_upsert(FakeClient(log), {"key": "k", "content": "v"})
        upsert_kwargs = [e[3] for e in log if e[1] == "upsert"]
        check("core_config_upsert uses owner_id,key in tenant mode",
              any(k.get("on_conflict") == "owner_id,key" for k in upsert_kwargs),
              f"on_conflict={[k.get('on_conflict') for k in upsert_kwargs]}")

    with patch.object(db_mod, "tenant_mode_enabled", return_value=False), \
         patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(log)):
        log.clear()
        db_mod.core_config_upsert(FakeClient(log), {"key": "k", "content": "v"})
        upsert_kwargs = [e[3] for e in log if e[1] == "upsert"]
        check("core_config_upsert uses key in legacy mode",
              any(k.get("on_conflict") == "key" for k in upsert_kwargs),
              f"on_conflict={[k.get('on_conflict') for k in upsert_kwargs]}")

    # [5] resolve_telegram_chat_id: per-user value wins; env fallback only in
    # single-user world
    with patch.object(db_mod, "get_tenant", return_value=first_uid), \
         patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(
             log, {"users": {"id": first_uid, "telegram_chat_id": "999999999"}})), \
         patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111111111"}, clear=False), \
         patch.object(db_mod, "active_user_ids", return_value=[first_uid]):
        resolved = db_mod.resolve_telegram_chat_id(first_uid)
    check("resolve_telegram_chat_id prefers users.telegram_chat_id",
          resolved == "999999999", f"resolved={resolved!r}")

    with patch.object(db_mod, "get_tenant", return_value=first_uid), \
         patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(
             log, {"users": {"id": first_uid, "telegram_chat_id": None}})), \
         patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111111111"}, clear=False), \
         patch.object(db_mod, "active_user_ids", return_value=[first_uid]):
        resolved_env = db_mod.resolve_telegram_chat_id(first_uid)
    check("single-user world falls back to env TELEGRAM_CHAT_ID",
          resolved_env == "111111111", f"resolved={resolved_env!r}")

    with patch.object(db_mod, "get_tenant", return_value=first_uid), \
         patch.object(db_mod, "get_supabase", side_effect=lambda: FakeClient(
             log, {"users": {"id": first_uid, "telegram_chat_id": None}})), \
         patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111111111"}, clear=False), \
         patch.object(db_mod, "active_user_ids", return_value=[first_uid, "00000000-0000-0000-0000-000000000002"]):
        resolved_none = db_mod.resolve_telegram_chat_id(first_uid)
    check("multi-user without per-user chat → None (no cross-tenant leak)",
          resolved_none is None, f"resolved={resolved_none!r}")

    # [6] push_notification scoped_tokens_query owner filter when tenant set
    import core.services.push_notification as pn_mod
    with patch.object(pn_mod, "get_tenant", return_value=first_uid), \
         patch.object(pn_mod, "get_supabase", side_effect=lambda: FakeClient(log)):
        log.clear()
        pn_mod.scoped_tokens_query(pn_mod.get_tenant())
        eqs = [a for e in log if e[1] == "eq" for a in [e[2]]]
        check("push scoped_tokens_query carries owner filter when tenant set",
              any(a and a[0] == "owner_id" and a[1] == first_uid for a in eqs),
              f"eqs={eqs}")

    # [7] audit_logger stamps owner_id from tenant context
    import core.lib.audit_logger as al_mod
    with patch.object(db_mod, "get_tenant", return_value=first_uid):
        stamped = al_mod._owner_attr()
    check("audit_logger._owner_attr resolves tenant", stamped == first_uid, f"owner={stamped!r}")

    print()
    if FAILURES:
        print(f"❌ M4 GATE FAILED — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ ALL M4 CRON FAN-OUT GATES PASSED")


if __name__ == "__main__":
    main()
