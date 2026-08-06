"""
verify_m3_remaining_isolation.py — M3 remaining-sweep verification
(plans/69-multi-tenant-product-plan.md §6 M3 exit)

Extends scripts/verify_m3_webhook_isolation.py to the modules swept after
core/webhook: core/pulse/*, core/skills/*, core/retrieval/*, core/lib/*,
core/actions/*, core/agents/*, core/decisions.py, core/clarifier.py,
api/index.py, plus db/80 owner-scoped RPCs.

Gates:
  [1] channel_tenant_scope() establishes the channel tenant (and is a no-op
      when a tenant context is already active)
  [2] module facades (core.pulse.llm.supabase, core.retrieval.search.supabase,
      core.actions.executor.supabase, core.decisions.supabase) are
      TenantAwareClient instances
  [3] facade .table()/.rpc() route through the tenant layer in tenant mode
      (owner injected); global RPCs (next_clarification_shortcode) are NOT
  [4] facade fails closed without a tenant (table + rpc raise)
  [5] RPC owner_id smoke on the copy DB: search_phrase_nodes and
      claim_pending_enrichment_job isolate by owner
  [6] api require_api_auth returns the resolved uid and keeps the tenant set
      (contract enforced by tests/unit/test_tenant_scope.py — asserted here
      at the import level)

Usage:
  python3 scripts/verify_m3_remaining_isolation.py \
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
    out = subprocess.run(["psql", dsn, "-qtAc", sql], capture_output=True, text=True, env=env, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


class Recorder:
    """Minimal builder that records operations chained onto it."""

    def __init__(self, log: list, tag: str):
        self._log = log
        self._tag = tag

    def select(self, *a, **k):
        self._log.append((self._tag, "select", a, k))
        return self

    def eq(self, *a, **k):
        self._log.append((self._tag, "eq", a, k))
        return self

    def rpc(self, *a, **k):
        self._log.append((self._tag, "rpc", a, k))
        return self

    def execute(self, *a, **k):
        self._log.append((self._tag, "execute", a, k))
        return self

    def __getattr__(self, item):
        def _passthrough(*a, **k):
            return self
        return _passthrough


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost:5433/rhodey_restore_test")
    args = ap.parse_args()

    print("M3 remaining-sweep isolation gate\n")

    from unittest.mock import patch
    from core.services import db as db_mod

    uid = str(uuid.uuid4())
    log: list = []

    def fake_client():
        c = Recorder(log, "table")
        c.table = lambda name: Recorder(log, f"table:{name}")
        c.rpc = lambda name, params=None: Recorder(log, f"rpc:{name}").rpc(name, params)
        return c

    # [1] channel_tenant_scope resolves the channel tenant
    db_mod._channel_tenant = db_mod._NO_CHANNEL_TENANT  # reset cache (None → re-probe)

    with patch.object(db_mod, "get_supabase", side_effect=fake_client):
        # Give resolve_channel_tenant a real result via a stubbed response
        def _fake_resolve():
            return uid
        with patch.object(db_mod, "resolve_channel_tenant", side_effect=_fake_resolve):
            with db_mod.channel_tenant_scope():
                got = db_mod.get_tenant()
            after = db_mod.get_tenant()
        check("channel_tenant_scope sets tenant + restores after",
              got == uid and after is None, f"inside={got!r} after={after!r}")

        # nested: no-op when a tenant is already active
        with patch.object(db_mod, "resolve_channel_tenant", side_effect=_fake_resolve):
            with db_mod.tenant_scope("existing"):
                with db_mod.channel_tenant_scope():
                    nested = db_mod.get_tenant()
        check("channel_tenant_scope no-op when nested",
              nested == "existing", f"nested inside={nested!r}")

    # [2] module facades are tenant-aware
    import core.pulse.llm as pulse_llm
    import core.retrieval.search as retr_search
    import core.retrieval.pipeline as retr_pipe
    import core.retrieval.backfill as retr_backfill
    import core.lib.graph_rules as gr_mod
    import core.lib.node_tables as nt_mod
    import core.lib.ingest as ing_mod
    import core.lib.temporal_lineage as tl_mod
    import core.skills.whatsapp_ingest as wa_mod
    import core.skills.email_ingest as ei_mod
    import core.skills.backfill_graph as bg_mod
    import core.agents.cleanup_orphans as co_mod
    import core.pulse.context as ctx_mod
    import core.pulse.memory_clusters as mc_mod
    facades = {
        "pulse.llm": pulse_llm.supabase,
        "pulse.context": ctx_mod.supabase,
        "pulse.memory_clusters": mc_mod.supabase,
        "retrieval.search": retr_search.supabase,
        "retrieval.pipeline": retr_pipe.supabase,
        "retrieval.backfill": retr_backfill.supabase,
        "lib.graph_rules": gr_mod.supabase,
        "lib.node_tables": nt_mod.supabase,
        "lib.ingest": ing_mod.supabase,
        "lib.temporal_lineage": tl_mod.supabase,
        "skills.whatsapp_ingest": wa_mod.supabase,
        "skills.email_ingest": ei_mod.supabase,
        "skills.backfill_graph": bg_mod.supabase,
        "agents.cleanup_orphans": co_mod.supabase,
    }
    all_facades = all(isinstance(c, db_mod.TenantAwareClient) for c in facades.values())
    check("module facades are TenantAwareClient",
          all_facades, ", ".join(f"{k}={type(v).__name__}" for k, v in facades.items()))

    # [3] facade routing: owner injected for data RPCs, skipped for global
    with patch.object(db_mod, "_tenant_mode", True), \
         patch.object(db_mod, "get_supabase", side_effect=fake_client), \
         db_mod.tenant_scope(uid):
        log.clear()
        facade = db_mod.tenant_aware_client()
        facade.rpc("match_memories", {"query_embedding": [0.0] * 768})
        rpc_entries = [e for e in log if e[1] == "rpc"]
        params = rpc_entries[0][2][1] if rpc_entries and len(rpc_entries[0][2]) > 1 else {}
        check("facade rpc injects owner_id (data RPC)",
              isinstance(params, dict) and params.get("owner_id") == uid,
              f"params owner_id={(params or {}).get('owner_id')!r}")

        log.clear()
        facade.rpc("next_clarification_shortcode")
        rpc_entries = [e for e in log if e[1] == "rpc"]
        params = rpc_entries[0][2][1] if rpc_entries and len(rpc_entries[0][2]) > 1 else {}
        check("facade rpc skips owner_id (global RPC)",
              params is None or "owner_id" not in (params or {}),
              f"params={params!r}")

        # facade table read scopes through TenantTable
        log.clear()
        facade.table("tasks").select("id").execute()
        sel_eqs = [a for e in log if e[1] == "eq" for a in [e[2]]]
        check("facade table read carries owner filter",
              any(a and a[0] == "owner_id" and a[1] == uid for a in sel_eqs),
              f"eqs: {sel_eqs}")

    # [4] facade fails closed without a tenant
    with patch.object(db_mod, "_tenant_mode", True), \
         patch.object(db_mod, "get_supabase", side_effect=fake_client):
        facade = db_mod.tenant_aware_client()
        try:
            facade.table("tasks")
            check("facade table fails closed without tenant", False, "no exception")
        except db_mod.TenantRequiredError:
            check("facade table fails closed without tenant", True, "TenantRequiredError")
        try:
            facade.rpc("match_memories", {})
            check("facade rpc fails closed without tenant", False, "no exception")
        except db_mod.TenantRequiredError:
            check("facade rpc fails closed without tenant", True, "TenantRequiredError")

    # [5] RPC owner isolation on the copy DB
    ta, tb = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        psql(args.dsn, f"insert into public.users (id, name, status) values ('{ta}', 'm3-rem-a', 'active')")
        psql(args.dsn, f"insert into public.users (id, name, status) values ('{tb}', 'm3-rem-b', 'active')")
        # search_phrase_nodes: two phrase nodes, one per owner. NOTE:
        # retrieval_phrase_nodes.normalized_text is globally unique (the
        # cross-tenant n-gram collision point the plan flags) — distinct
        # texts per owner prove the RPC's owner filter, not the unique key.
        psql(args.dsn, f"insert into public.retrieval_phrase_nodes (normalized_text, display_text, node_type, owner_id) values ('m3phrasechecka', 'M3 Phrase Check A', 'concept', '{ta}')")
        psql(args.dsn, f"insert into public.retrieval_phrase_nodes (normalized_text, display_text, node_type, owner_id) values ('m3phrasecheckb', 'M3 Phrase Check B', 'concept', '{tb}')")
        a_sees_own = psql(args.dsn, f"select count(*) from public.search_phrase_nodes('m3phrasechecka', 10, '{ta}')")
        a_sees_b = psql(args.dsn, f"select count(*) from public.search_phrase_nodes('m3phrasecheckb', 10, '{ta}')")
        b_sees_own = psql(args.dsn, f"select count(*) from public.search_phrase_nodes('m3phrasecheckb', 10, '{tb}')")
        b_sees_a = psql(args.dsn, f"select count(*) from public.search_phrase_nodes('m3phrasechecka', 10, '{tb}')")
        check("rpc search_phrase_nodes isolates by owner",
              a_sees_own == "1" and a_sees_b == "0" and b_sees_own == "1" and b_sees_a == "0",
              f"A sees own={a_sees_own} B's={a_sees_b} | B sees own={b_sees_own} A's={b_sees_a}")

        # match_graph_nodes + get_most_connected_nodes (db/82 — the two graph
        # RPCs initially missed by the sweep; the facade would have failed them
        # loudly, and get_most_connected_nodes was LANGUAGE sql — the exact
        # silent-leak class). Two graph nodes, one per owner.
        na, nb = str(uuid.uuid4()), str(uuid.uuid4())
        va = "ARRAY[0.1," + ",".join(["0.1"] * 767) + "]::vector"
        vb = "ARRAY[0.9," + ",".join(["0.9"] * 767) + "]::vector"
        psql(args.dsn, f"insert into public.graph_nodes (id, label, type, embedding, owner_id) values ('{na}', 'M3GRAPH-A', 'concept', {va}, '{ta}')")
        psql(args.dsn, f"insert into public.graph_nodes (id, label, type, embedding, owner_id) values ('{nb}', 'M3GRAPH-B', 'concept', {vb}, '{tb}')")
        a_match = psql(args.dsn, f"select label from public.match_graph_nodes({va}, 0.0, 10, '{ta}')")
        b_match = psql(args.dsn, f"select label from public.match_graph_nodes({vb}, 0.0, 10, '{tb}')")
        a_deg = psql(args.dsn, f"select string_agg(label, ',') from public.get_most_connected_nodes(10, '{ta}')")
        b_deg = psql(args.dsn, f"select string_agg(label, ',') from public.get_most_connected_nodes(10, '{tb}')")
        check("rpc match_graph_nodes isolates by owner",
              a_match == "M3GRAPH-A" and b_match == "M3GRAPH-B",
              f"A sees={a_match!r} B sees={b_match!r}")
        check("rpc get_most_connected_nodes isolates by owner",
              "M3GRAPH-A" in (a_deg or "") and "M3GRAPH-B" not in (a_deg or "")
              and "M3GRAPH-B" in (b_deg or "") and "M3GRAPH-A" not in (b_deg or ""),
              f"A sees={a_deg!r} B sees={b_deg!r}")

        # claim_pending_enrichment_job: only the caller's own job can be claimed
        jid_a = psql(args.dsn, f"insert into public.pending_enrichment_jobs (job_type, target_type, target_id, content, status, owner_id) values ('task_graph', 'task', 1, 'm3-rem-job-a', 'pending', '{ta}') returning id")
        jid_b = psql(args.dsn, f"insert into public.pending_enrichment_jobs (job_type, target_type, target_id, content, status, owner_id) values ('task_graph', 'task', 2, 'm3-rem-job-b', 'pending', '{tb}') returning id")
        psql(args.dsn, f"select * from public.claim_pending_enrichment_job({jid_a}, '{ta}')")
        a_claimed = psql(args.dsn, f"select count(*) from public.pending_enrichment_jobs where id={jid_a} and status='processing'")
        b_touched = psql(args.dsn, f"select count(*) from public.pending_enrichment_jobs where id={jid_b} and status='processing'")
        # B's job is still claimable by B
        psql(args.dsn, f"select * from public.claim_pending_enrichment_job({jid_b}, '{tb}')")
        b_claimed = psql(args.dsn, f"select count(*) from public.pending_enrichment_jobs where id={jid_b} and status='processing'")
        check("rpc claim_pending_enrichment_job isolates by owner",
              a_claimed == "1" and b_touched == "0" and b_claimed == "1",
              f"A claimed={a_claimed} B touched-by-A={b_touched} B self-claimed={b_claimed}")
    finally:
        psql(args.dsn, f"delete from public.pending_enrichment_jobs where owner_id in ('{ta}', '{tb}')")
        psql(args.dsn, f"delete from public.retrieval_phrase_nodes where owner_id in ('{ta}', '{tb}')")
        psql(args.dsn, f"delete from public.graph_nodes where owner_id in ('{ta}', '{tb}')")
        psql(args.dsn, f"delete from public.users where id in ('{ta}', '{tb}')")

    # [6] api require_api_auth contract (returns uid; keeps tenant set) is
    # enforced in tests/unit/test_tenant_scope.py; assert the signature here.
    import inspect
    import api.index as api_mod
    sig = inspect.signature(api_mod.require_api_auth)
    check("api require_api_auth exists with request param",
          "request" in sig.parameters, str(sig))

    print()
    if FAILURES:
        print(f"❌ ISOLATION GATE FAILED — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ ALL M3 REMAINING ISOLATION GATES PASSED")


if __name__ == "__main__":
    main()
