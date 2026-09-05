"""Evidence-gate + provenance UAT — TEST TENANT ONLY.

Verifies the P1+P2 hardening (core/lib/entity_context.py) end-to-end against
the dedicated Test tenant's real DB:

  A. Baseline snapshot + known-entity USER MESSAGE dry-run → extraction is
     read-only; live entities resolve; ZERO new pending rows.
  B. NEW-entity USER MESSAGE (real extraction, LLM phase) → decision-gated
     queue_pending_candidates WITH provenance → the created pending rows
     carry provenance {origin_table, origin_id}; nothing is dropped.
  C. Junk-label messages (the 1009 family: Please / Chief / Staff / News /
     Update / Meeting / Call) → the gate REJECTS every one at the real DB —
     zero new pending rows, regardless of caller.
  D. LLM-only minimum evidence — a pure-LLM guess without any structural
     signal must not materialize; an entity-like LLM-only candidate must
     (and must carry provenance).
  E. Dedup idempotency — queuing the same candidate twice yields ONE row.
  F. Owner-scope + leak check + baseline restore + full cleanup.

SAFETY (non-negotiable, mirrors uat_hitl_hardening.py):
  - Tenant resolved via tests.fixtures.test_tenant.resolve_test_tenant_uid()
    and HARD-FAILED unless the user's name is exactly "Test".
  - Every call runs inside tenant_scope(TEST_UID) so the TenantTable facade
    owner-scopes every insert.
  - All verification queries + cleanup are scoped eq(owner_id, TEST_UID);
    cleanup deletes only rows created by this run (run-stamp / captured ids).

Run:  python3 scripts/uat_evidence_gate.py
"""

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import tenant_scope, get_tenant  # noqa: E402

RUN_STAMP = f"UATGT{int(time.time())}"

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


def provenance_rows(sb, owner: str, label_like: str) -> list:
    rows = sb.table("pending_nodes").select("id, label, node_type, status, provenance").eq(
        "owner_id", owner).ilike("label", label_like).execute()
    return rows.data or []


async def main():
    global PASS, FAIL
    T = test_tenant()
    sb = None
    created_pending_ids = []

    try:
        from tests.fixtures.test_tenant import fresh_supabase
        sb = fresh_supabase()

        baseline = pending_snapshot(sb, T)
        print(f"\n📊 Baseline: {len(baseline)} pending/flagged rows (test tenant)")

        # ── A. Known-entity user message: extraction is read-only ──────────
        print("\n[Scenario A] Known-entity USER MESSAGE (dry-run extraction, read-only)")
        msg_a = "Had a productive call with Tom Okafor about the Solstice Labs expansion"
        from core.lib.entity_context import extract_context_from_source, queue_pending_candidates
        with tenant_scope(T):
            assert get_tenant() == T
            ctx_a = await extract_context_from_source(msg_a, timing="card", owner_id=T)
        check(ctx_a.organization_id is not None,
              "known org resolved live (Solstice Labs)", f"org_id={ctx_a.organization_id}")
        check(ctx_a.organization_name == "Solstice Labs",
              "org label = Solstice Labs", str(ctx_a.organization_name))
        check(ctx_a.pending_org_id is None, "no pending org id from extraction")
        check(any("Tom Okafor" in n for n in ctx_a.person_names),
              "known person detected", f"persons={ctx_a.person_names}")
        check(pending_snapshot(sb, T) == baseline, "ZERO new pending rows after extraction")

        # ── B. NEW-entity user message → gate with provenance (real DB) ────
        print("\n[Scenario B] NEW-entity USER MESSAGE → queue WITH provenance")
        msg_b = (f"Follow up with Meera Kulkarni from Celestine Partners about the "
                 f"{RUN_STAMP} audit — she is the CFO there")
        with tenant_scope(T):
            ctx_b = await extract_context_from_source(msg_b, timing="card", owner_id=T)
            queue_pending_candidates(ctx_b, owner_id=T, provenance={
                "origin_table": "messages", "origin_id": f"uat-msg-{RUN_STAMP}",
            })

        fresh_b = pending_snapshot(sb, T) - baseline
        check(len(fresh_b) >= 1,
              "gate materialized ≥1 pending row from the new-entity message",
              f"new={sorted(fresh_b)}")
        # The LLM phase may trim/split labels, so assert on the CREATED ROWS
        # (snapshot delta), not on guessed labels.
        if fresh_b:
            created_pending_ids.extend(sorted(fresh_b))
            b_rows = sb.table("pending_nodes").select(
                "id, label, node_type, status, provenance"
            ).eq("owner_id", T).in_("id", sorted(fresh_b)).execute().data or []
            org_b = [r for r in b_rows if r.get("node_type") == "organization"]
            person_b = [r for r in b_rows if r.get("node_type") == "person"]
            check(len(org_b) == 1, "exactly one org row materialized",
                  f"{[(r['id'], r['label']) for r in org_b]}")
            check(len(person_b) >= 1, "≥1 person row materialized",
                  f"{[(r['id'], r['label']) for r in person_b]}")
            for r in b_rows:
                check(r["status"] == "pending", f"{r['label']}: status=pending", str(r["status"]))
                prov = r.get("provenance")
                check(bool(prov), f"{r['label']}: provenance populated", f"{prov!r}")
                if prov:
                    try:
                        p = json.loads(prov)
                        ok_p = (p.get("origin_table") == "messages"
                                and p.get("origin_id") == f"uat-msg-{RUN_STAMP}")
                        check(ok_p, f"{r['label']}: provenance carries origin_table/origin_id", str(p))
                    except Exception as e:
                        check(False, f"{r['label']}: provenance parses as JSON", str(e))
        else:
            check(False, "no rows created — inspect message/extraction", msg_b)

        # ── C. Junk-label family — deterministic rejection at the real DB ──
        print("\n[Scenario C] Junk-label family (the 1009 class) — must be REJECTED")
        from core.lib.entity_context import _create_pending_org, _create_pending_person

        before_c = pending_snapshot(sb, T)
        junk = ["Please", "Chief", "Staff", "News", "Update", "Meeting", "Call", "Email"]
        rejected = 0
        for label in junk:
            with tenant_scope(T):
                r_org = _create_pending_org(label, f"user message with {label}", owner_id=T,
                                            provenance={"origin_table": "messages", "origin_id": f"junk-{label}"})
                r_person = _create_pending_person(
                    label, f"user message with {label}", owner_id=T,
                    provenance={"origin_table": "messages", "origin_id": f"junk-{label}"},
                    detected_entities=[{"type": "person", "label": "Someone Else", "confidence": 0.9}],
                    person_names=[],
                )
            if r_org is None and r_person is None:
                rejected += 1
            else:
                print(f"  ❌  '{label}' NOT rejected: org={r_org} person={r_person}")
        check(rejected == len(junk), "ALL junk labels rejected by the evidence gate",
              f"{rejected}/{len(junk)}")
        after_c = pending_snapshot(sb, T)
        check(after_c == before_c, "ZERO new pending rows from the junk messages",
              f"delta={sorted(after_c - before_c)}")

        # ── D. LLM-only minimum evidence ───────────────────────────────────
        print("\n[Scenario D] LLM-only minimum evidence")
        # Pure-LLM guess with NO deterministic signal and NO entity-like label → rejected
        with tenant_scope(T):
            r_d1 = _create_pending_person(
                "Please",
                "source text",
                owner_id=T,
                detected_entities=[{"type": "person", "label": "Someone Else", "confidence": 0.9}],
                person_names=[],
            )
        check(r_d1 is None, "LLM-only common-word guess rejected (no row, no exception)")
        # LLM-only candidate with entity-like label quality → created with provenance
        token_label = f"Zyx{int(time.time()) % 1000000}"  # single capitalized token, unique
        with tenant_scope(T):
            r_d2 = _create_pending_person(
                token_label,
                "source text",
                owner_id=T,
                provenance={"origin_table": "messages", "origin_id": f"uat-llm-{RUN_STAMP}"},
                detected_entities=[{"type": "person", "label": "Someone Else", "confidence": 0.9}],
                person_names=[],
            )
        check(r_d2 is not None, "LLM-only entity-like candidate created",
              f"pending_id={r_d2}")
        if r_d2:
            created_pending_ids.append(r_d2)
            prow = sb.table("pending_nodes").select("provenance, node_type").eq("owner_id", T).eq("id", r_d2).limit(1).execute()
            if prow.data:
                check(prow.data[0]["node_type"] == "person", "LLM-only row node_type=person")
                check(bool(prow.data[0].get("provenance")), "LLM-only row carries provenance")

        # ── E. Dedup idempotency ───────────────────────────────────────────
        print("\n[Scenario E] Dedup idempotency — queue twice → ONE row")
        msg_e = f"Follow up with the Apex Harbinger finance team about the {RUN_STAMP} closing"
        with tenant_scope(T):
            ctx_e = await extract_context_from_source(msg_e, timing="card", owner_id=T)
            queue_pending_candidates(ctx_e, owner_id=T, provenance={
                "origin_table": "messages", "origin_id": f"uat-dedup-{RUN_STAMP}",
            })
        fresh_e1 = pending_snapshot(sb, T) - baseline - set(created_pending_ids)
        with tenant_scope(T):
            ctx_e2 = await extract_context_from_source(msg_e, timing="card", owner_id=T)
            queue_pending_candidates(ctx_e2, owner_id=T, provenance={
                "origin_table": "messages", "origin_id": f"uat-dedup-{RUN_STAMP}",
            })
        fresh_e2 = pending_snapshot(sb, T) - baseline - set(created_pending_ids)
        check(len(fresh_e1) >= 1, "first queue created ≥1 pending row",
              f"new={sorted(fresh_e1)}")
        check(fresh_e2 == fresh_e1,
              "second queue created NO additional rows (idempotent dedup)",
              f"delta={sorted(fresh_e2 - fresh_e1)}")
        if fresh_e2:
            created_pending_ids.extend(sorted(fresh_e2))

        # ── F. Owner-scope + baseline restore ──────────────────────────────
        print("\n[Scenario F] Owner-scope + baseline restore")
        leaked = []
        for pid in created_pending_ids:
            r = sb.table("pending_nodes").select("owner_id").eq("id", pid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"pending {pid}")
        check(not leaked, "all created pending rows carry the Test owner_id", "; ".join(leaked) or "clean")

    finally:
        if sb is not None:
            for pid in created_pending_ids:
                try:
                    sb.table("pending_nodes").delete().eq("owner_id", T).eq("id", pid).execute()
                except Exception:
                    pass
            # Stray stamped rows (defensive; org/person labels embed the run stamp)
            try:
                rows = sb.table("pending_nodes").select("id").eq("owner_id", T).ilike("label", f"%{RUN_STAMP}%").execute()
                for r in (rows.data or []):
                    try:
                        sb.table("pending_nodes").delete().eq("owner_id", T).eq("id", r["id"]).execute()
                    except Exception:
                        pass
            except Exception:
                pass
            final = pending_snapshot(sb, T)
            try:
                print(f"\n🧹 Cleanup done — {len(created_pending_ids)} rows removed; "
                      f"pending set == baseline: {final == baseline}")
            except Exception:
                pass

    print("\n" + "=" * 60)
    print(f"EVIDENCE-GATE UAT RESULT: {PASS} passed, {FAIL} failed  [tenant={T[:8]}…]")
    print("=" * 60)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
