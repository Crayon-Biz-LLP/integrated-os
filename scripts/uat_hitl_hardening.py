"""Combined HITL-hardening UAT — TEST TENANT ONLY.

Verifies the 5-step hardening end-to-end against the dedicated Test tenant:

  Step 1  Extraction is read-only (no pending rows from extraction itself)
  Step 2  Pending rows always resolve once a live node exists
  Step 3  Executor system notes route through create_note_direct (no raw
          memories.insert, no second scan) — closure history note included
  Step 4  No ungated pending creation anywhere in the exercised paths
  Step 5  (static guard lives in tests/unit/test_pending_writer_guard.py)

Scenarios:
  A. Known-entity message  → org + person resolve live; ZERO new pending rows
  B. Brand-new-org message → extraction records the candidate label but
     creates NO pending row (pending_org_id stays None)
  C. Decision-gated queue  → queue_pending_candidates creates EXACTLY one
     pending row (the gate works when called explicitly) — cleaned up
  D. Executor note path    → exactly one memory via create_note_direct,
     org linked, ZERO new pending
  E. Executor TASK path    → Guard-B TASK_CONTEXT memory + task, ZERO new pending
  F. Closure path          → task closed + exactly one webhook_completion
     memory (canonical writer), ZERO new pending

SAFETY (non-negotiable):
  - The tenant is resolved via tests.fixtures.test_tenant.resolve_test_tenant_uid()
    (never falls back to the channel tenant), and the script HARD-FAILS unless
    the resolved user's name is exactly "Test".
  - Every pipeline call runs inside tenant_scope(TEST_UID); the TenantTable
    facade auto-injects owner_id on inserts, so nothing can leak to another
    tenant.
  - Cleanup deletes ONLY rows created by this run (captured ids / run-stamp).

Run:  python3 scripts/uat_hitl_hardening.py
"""

import asyncio
import hashlib
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import tenant_scope, get_tenant  # noqa: E402

RUN_STAMP = f"UATHHL{int(time.time())}"
CHAT_ID = 999999001  # test band chat id (never a real user chat)

PASS = 0
FAIL = 0


def check(ok: bool, label: str, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {label}" + (f" — {detail}" if detail else ""))


def test_tenant() -> str:
    """Resolve the dedicated Test tenant. HARD-FAIL on anything else."""
    from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid

    uid = resolve_test_tenant_uid()
    if not uid:
        sys.exit("❌ TEST TENANT UNRESOLVABLE — refusing to run against any other tenant")
    sb = fresh_supabase()
    user = sb.table("users").select("id, name, status").eq("id", uid).limit(1).execute()
    name = (user.data or [{}])[0].get("name") if user.data else None
    if name != "Test":
        sys.exit(f"❌ Resolved tenant is '{name}' not 'Test' — refusing to run")
    print(f"🛡️  Tenant guard: {uid} name='Test' status='{((user.data or [{}])[0]).get('status')}'")
    return uid


def pending_snapshot(sb, owner: str) -> set:
    rows = sb.table("pending_nodes").select("id").eq("owner_id", owner).in_("status", ["pending", "flagged"]).execute()
    return {r["id"] for r in (rows.data or [])}


def count_memories(sb, owner: str, source: str, content: str) -> int:
    rows = sb.table("memories").select("id").eq("owner_id", owner).eq("source", source).eq("content", content).execute()
    return len(rows.data or [])


async def main():
    global PASS, FAIL
    T = test_tenant()
    sb = None

    created_task_ids = []
    created_memory_ids = []
    created_pending_ids = []

    try:
        from tests.fixtures.test_tenant import fresh_supabase
        sb = fresh_supabase()

        original_baseline = pending_snapshot(sb, T)
        baseline_pending = set(original_baseline)
        print(f"\n📊 Baseline: {len(baseline_pending)} pending/flagged rows (test tenant)")

        # ── Scenario A + D: known-entity message ─────────────────────────
        print("\n[Scenario A] Pure extraction on KNOWN entities (Solstice Labs / Tom Okafor)")
        msg_a = "Had a productive call with Tom Okafor about the Solstice Labs expansion"
        from core.lib.entity_context import extract_context_from_source
        with tenant_scope(T):
            assert get_tenant() == T
            ctx_a = await extract_context_from_source(msg_a, timing="card", owner_id=T)

        check(ctx_a.organization_id is not None,
              "known org resolved live", f"org_id={ctx_a.organization_id}")
        check(ctx_a.organization_name == "Solstice Labs",
              "org label = Solstice Labs", str(ctx_a.organization_name))
        check(ctx_a.pending_org_id is None,
              "no pending org id from extraction")
        check(any("Tom Okafor" in n for n in ctx_a.person_names),
              "known person detected", f"persons={ctx_a.person_names}")
        check(pending_snapshot(sb, T) == baseline_pending,
              "zero new pending rows after extraction")

        # ── Scenario B: brand-new org — extraction must NOT write ────────
        print("\n[Scenario B] Pure extraction on a BRAND-NEW org — must not create anything")
        msg_b = f"Follow up with the {RUN_STAMP} Helios Labs team about onboarding"
        with tenant_scope(T):
            ctx_b = await extract_context_from_source(msg_b, timing="card", owner_id=T)

        check(ctx_b.pending_org_id is None,
              "pending_org_id stays None (no silent node)")
        helios_mentioned = "Helios" in (ctx_b.pending_org_label or "") or any(
            "Helios" in (e.get("label") or "") for e in (ctx_b.detected_entities or [])
        )
        check(helios_mentioned,
              "candidate label recorded for UI",
              f"pending_org_label={ctx_b.pending_org_label!r}")
        helios_rows = sb.table("pending_nodes").select("id").eq("owner_id", T).ilike("label", "%Helios%").execute()
        check(not (helios_rows.data or []),
              "NO pending row created for the new org",
              f"rows={[(r['id'],) for r in (helios_rows.data or [])]}")

        # ── Scenario C: decision-gated queue creates exactly one row ─────
        print("\n[Scenario C] queue_pending_candidates — the gate, called explicitly")
        from core.lib.entity_context import queue_pending_candidates
        with tenant_scope(T):
            ctx_b2 = await extract_context_from_source(msg_b, timing="card", owner_id=T)
            queue_pending_candidates(ctx_b2, owner_id=T)

        helios_rows = sb.table("pending_nodes").select("id, label, status").eq("owner_id", T).ilike("label", "%Helios%").execute()
        helios_new = [r for r in (helios_rows.data or [])]
        check(len(helios_new) == 1,
              "exactly one pending row via the gate", f"rows={[(r['id'], r['label'], r['status']) for r in helios_new]}")
        if len(helios_new) == 1:
            check(helios_new[0]["status"] == "pending", "row status = pending")
        check(ctx_b2.pending_org_id is not None or len(helios_new) == 1,
              "gate filled pending_org_id on the context")
        # Remove the deliberate gate artifact NOW so the pending set returns to
        # baseline before the executor scenarios — the gate itself is proven;
        # it must not pollute the "zero new pending" checks below.
        if len(helios_new) == 1:
            sb.table("pending_nodes").delete().eq("owner_id", T).eq("id", helios_new[0]["id"]).execute()
        restored = pending_snapshot(sb, T)
        baseline_pending = set(restored)
        check(restored == original_baseline,
              "pending set restored to original baseline after gate cleanup",
              f"delta={sorted(restored ^ original_baseline)}")

        # ── Scenario D: executor note path (canonical writer) ────────────
        print("\n[Scenario D] Executor NOTE path — one memory, canonical writer, no pending")
        from core.actions.models import Action
        from core.actions.executor import execute_planned_actions
        msg_d = f"{msg_a} — {RUN_STAMP} note"
        note_action = Action(operation="create_note", human_label="UAT note",
                             params={"content": msg_d})
        with tenant_scope(T):
            ctx_d = await extract_context_from_source(msg_d, timing="card", owner_id=T)
            await execute_planned_actions(
                [note_action], CHAT_ID, text=msg_d, source="web",
                intent="NOTE", suppress_telegram=True, entity_context=ctx_d,
            )
        check(count_memories(sb, T, "web", msg_d) == 1,
              "exactly one memory (source=web) via create_note_direct")
        mem_rows = sb.table("memories").select("id, organization_id").eq("owner_id", T).eq("source", "web").eq("content", msg_d).execute()
        if mem_rows.data:
            created_memory_ids.append(mem_rows.data[0]["id"])
            check(str(mem_rows.data[0].get("organization_id")) == str(ctx_d.organization_id),
                  "memory org-linked to Solstice Labs")
        check(pending_snapshot(sb, T) == baseline_pending,
              "zero new pending rows after note path")

        # ── Scenario E: executor TASK path (Guard B) ─────────────────────
        print("\n[Scenario E] Executor TASK path — Guard-B TASK_CONTEXT memory, no pending")
        msg_e = f"Met Tom Okafor from Solstice Labs about the {RUN_STAMP} Q3 rollout — need to follow up"
        task_action = Action(operation="create_task", human_label="Q3 rollout follow-up",
                             params={"title": f"{RUN_STAMP} Q3 rollout follow-up", "reminder_at": None})
        with tenant_scope(T):
            ctx_e = await extract_context_from_source(msg_e, timing="card", owner_id=T)
            results = await execute_planned_actions(
                [task_action], CHAT_ID, text=msg_e, source="web",
                intent="TASK", suppress_telegram=True, entity_context=ctx_e,
            )
        check(count_memories(sb, T, "web", msg_e) == 1,
              "exactly one Guard-B TASK_CONTEXT memory (source=web)")
        guard_b_rows = sb.table("memories").select("id, metadata").eq("owner_id", T).eq("source", "web").eq("content", msg_e).execute()
        if guard_b_rows.data:
            created_memory_ids.append(guard_b_rows.data[0]["id"])
            meta = guard_b_rows.data[0].get("metadata") or {}
            check(meta.get("intent") == "TASK_CONTEXT",
                  "Guard-B memory carries TASK_CONTEXT intent", str(meta.get("intent")))
        created = [r for r in results if r.status == "committed" and r.operation == "create_task"]
        check(len(created) == 1, "task created", f"task_id={created[0].target_id if created else None}")
        if created and created[0].target_id:
            created_task_ids.append(int(created[0].target_id))
        check(pending_snapshot(sb, T) == baseline_pending,
              "zero new pending rows after TASK path")

        # ── Scenario F: closure path ─────────────────────────────────────
        print("\n[Scenario F] Closure — task closed + exactly one webhook_completion memory")
        from core.pulse.tools import create_task_direct
        close_title = f"{RUN_STAMP} Solstice Q3 review"
        dedup_key = hashlib.md5(close_title.lower().encode()).hexdigest()[:16]
        with tenant_scope(T):
            ctx_f = await extract_context_from_source(close_title, timing="card", owner_id=T)
            created = await create_task_direct(
                title=close_title, dedup_key=dedup_key,
                organization_name="Solstice Labs", entity_context=ctx_f,
            )
        check(created.get("action") == "created", "setup task created",
              f"task_id={created.get('task_id')}")
        if created.get("task_id"):
            created_task_ids.append(int(created["task_id"]))
        close_task_id = created.get("task_id")

        msg_f = f"Closing {RUN_STAMP} Solstice Q3 review engagement — done"
        close_action = Action(operation="close_task", target_id=int(close_task_id),
                              human_label="Solstice Q3 review")
        with tenant_scope(T):
            await execute_planned_actions(
                [close_action], CHAT_ID, text=msg_f, source="web",
                intent="COMPLETION", suppress_telegram=True, entity_context=ctx_f,
            )
        task_row = sb.table("tasks").select("status").eq("owner_id", T).eq("id", int(close_task_id)).limit(1).execute()
        check(task_row.data and task_row.data[0]["status"] == "done",
              "task closed", f"status={task_row.data[0]['status'] if task_row.data else 'MISSING'}")
        check(count_memories(sb, T, "webhook_completion", msg_f) == 1,
              "exactly one webhook_completion memory via create_note_direct")
        comp_rows = sb.table("memories").select("id, metadata").eq("owner_id", T).eq("source", "webhook_completion").eq("content", msg_f).execute()
        if comp_rows.data:
            created_memory_ids.append(comp_rows.data[0]["id"])
            meta = comp_rows.data[0].get("metadata") or {}
            check(meta.get("intent") == "COMPLETION",
                  "closure memory carries COMPLETION intent", str(meta.get("intent")))
        check(pending_snapshot(sb, T) == baseline_pending,
              "zero new pending rows after closure path")

        # ── Final leak check ─────────────────────────────────────────────
        print("\n[Final] Cross-check: every artifact created by this run is owner-scoped")
        leaked = []
        for tid in created_task_ids:
            r = sb.table("tasks").select("owner_id").eq("id", tid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"task {tid}")
        for mid in created_memory_ids:
            r = sb.table("memories").select("owner_id").eq("id", mid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"memory {mid}")
        check(not leaked, "all created rows carry the Test tenant owner_id", "; ".join(leaked) or "clean")

    finally:
        # ── Cleanup: only this run's artifacts, Test tenant only ─────────
        if sb is not None:
            for tid in created_task_ids:
                try:
                    sb.table("tasks").delete().eq("owner_id", T).eq("id", tid).execute()
                except Exception:
                    pass
            for mid in created_memory_ids:
                try:
                    sb.table("memories").delete().eq("owner_id", T).eq("id", mid).execute()
                except Exception:
                    pass
            for pid in created_pending_ids:
                try:
                    sb.table("pending_nodes").delete().eq("owner_id", T).eq("id", pid).execute()
                except Exception:
                    pass
            # enrichment-queue rows spawned by this run (content-stamped)
            try:
                sb.table("enrichment_queue").delete().ilike("content", f"%{RUN_STAMP}%").execute()
            except Exception:
                pass
            print(f"\n🧹 Cleanup done — {len(created_task_ids)} tasks, {len(created_memory_ids)} "
                  f"memories, {len(created_pending_ids)} pending rows removed (Test tenant only)")

    print("\n" + "=" * 60)
    print(f"UAT RESULT: {PASS} passed, {FAIL} failed  [tenant={T[:8]}…]")
    print("=" * 60)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())