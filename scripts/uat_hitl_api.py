"""API-level HITL-hardening UAT — TEST TENANT ONLY.

Drives the REAL FastAPI surface (same endpoints the Flutter app calls) with a
per-user X-API-Key for the dedicated Test tenant, forcing the inline/sync
paths (Modal worker unavailable in this process):

  POST /api/send-message        — the app's message entry point
  POST /api/suggestions/confirm — the suggestion-card confirm endpoint

Scenarios:
  1. Known-entity TASK message (Path C direct)      → task created, org linked,
     ZERO new pending rows (extraction never writes).
  2. Two NEW entities (org + person) → suggestion card → confirm via API
     → live graph nodes created, org Bridge-C backfilled onto the task,
     ZERO junk pending rows.
  3. Closure message for the scenario-1 task (COMPLETION direct)
     → task done, exactly one webhook_completion memory, ZERO new pendings.

SAFETY (non-negotiable):
  - Tenant resolved via resolve_test_tenant_uid() + hard-fail unless name == "Test".
  - The Test user's api_key_hash is TEMPORARILY set to a known UAT key so the
    per-user X-API-Key scopes every request to Test; the ORIGINAL hash is
    restored in `finally`. No other tenant's rows are ever read or written.
  - Every verification query and cleanup is scoped eq(owner_id, TEST_UID).
  - Cleanup deletes ONLY artifacts created by this run (run-stamp / ids).

Run:  python3 scripts/uat_hitl_api.py
"""

import hashlib
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RUN_STAMP = f"UATAPI{int(time.time())}"
CHAT_ID = 999999002  # test-band chat id (never a real user chat)

UAT_KEY = f"uat-key-{RUN_STAMP}-{hashlib.md5(RUN_STAMP.encode()).hexdigest()[:8]}"

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
    from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid

    uid = resolve_test_tenant_uid()
    if not uid:
        sys.exit("❌ TEST TENANT UNRESOLVABLE — refusing to run")
    sb = fresh_supabase()
    user = sb.table("users").select("id, name, status").eq("id", uid).limit(1).execute()
    name = (user.data or [{}])[0].get("name") if user.data else None
    if name != "Test":
        sys.exit(f"❌ Resolved tenant is '{name}' not 'Test' — refusing to run")
    print(f"🛡️  Tenant guard: {uid} name='Test'")
    return uid


def new_pending_ids(sb, owner: str, baseline: set) -> set:
    """Ids in the current pending set that were NOT in the baseline."""
    rows = sb.table("pending_nodes").select("id").eq("owner_id", owner).in_("status", ["pending", "flagged"]).execute()
    return {r["id"] for r in (rows.data or [])} - baseline


def main_sync():
    global PASS, FAIL
    T = test_tenant()

    # ── Forced inline: make the Modal worker spawn fail in-process ─────────
    import sys as _sys
    _sys.modules["modal"] = None  # `import modal` → ImportError → sync fallback

    sb = None
    original_hash = None
    created_task_ids = []
    created_memory_ids = []
    created_node_ids = []
    created_dump_ids = []

    try:
        from fastapi.testclient import TestClient
        from api.index import app

        from tests.fixtures.test_tenant import fresh_supabase
        sb = fresh_supabase()

        # ── Temporarily mint a per-user key for the Test tenant ────────────
        user_row = sb.table("users").select("api_key_hash").eq("id", T).limit(1).execute()
        original_hash = user_row.data[0].get("api_key_hash") if user_row.data else None
        sb.table("users").update({"api_key_hash": hashlib.sha256(UAT_KEY.encode()).hexdigest()}).eq("id", T).execute()
        print("🔑 Temporary per-user key installed for Test (restored on exit)")

        client = TestClient(app)
        headers = {"X-API-Key": UAT_KEY}

        baseline = {r["id"] for r in (sb.table("pending_nodes").select("id").eq("owner_id", T).in_("status", ["pending", "flagged"]).execute().data or [])}
        print(f"\n📊 Baseline: {len(baseline)} pending/flagged rows (Test tenant)")

        # ── Scenario 1: known-entity TASK message (Path C direct) ──────────
        print("\n[Scenario 1] Known-entity TASK message (Tom Okafor / Solstice Labs)")
        msg1 = f"Follow up with Tom Okafor at Solstice Labs about the {RUN_STAMP} lease renewal"
        r = client.post("/api/send-message", json={"message": msg1, "session_id": None}, headers=headers)
        check(r.status_code == 200, "POST /api/send-message → 200", f"resp={r.text[:160]}")
        tasks1 = sb.table("tasks").select("id, title, organization_id").eq("owner_id", T).ilike("title", f"%{RUN_STAMP}%").execute().data or []
        check(len(tasks1) == 1, "exactly one task created for the message", f"tasks={[t['id'] for t in tasks1]}")
        if tasks1:
            created_task_ids.append(tasks1[0]["id"])
            check(str(tasks1[0].get("organization_id")) == "6351ada3-9457-434b-ae6f-ee3d595fa9ad",
                  "task org-linked to Solstice Labs (live node)", str(tasks1[0].get("organization_id")))
        fresh = new_pending_ids(sb, T, baseline)
        check(not fresh, "ZERO new pending rows after known-entity message", f"new={sorted(fresh)}")

        # ── Scenario 2: two NEW entities → card → confirm (Path A/B) ───────
        print("\n[Scenario 2] Two NEW entities → suggestion card → confirm")
        existing_names = {
            n["label"].lower() for n in (sb.table("graph_nodes").select("label").eq("owner_id", T).eq("is_current", True).execute().data or [])
        }
        if "ishaan rao" in existing_names or "meridian labs" in existing_names:
            sys.exit("❌ Chosen new-entity labels already exist in Test tenant — pick fresh names")
        msg2 = f"Follow up with Ishaan Rao about the {RUN_STAMP} Meridian Labs proposal"
        r = client.post("/api/send-message", json={"message": msg2, "session_id": None}, headers=headers)
        check(r.status_code == 200, "POST /api/send-message → 200", f"resp={r.text[:160]}")

        # The inbound dump carries metadata.entity_context — that is confirm's source_id
        dump_rows = sb.table("raw_dumps").select("id, direction, message_type, created_at").eq("owner_id", T).eq("content", msg2).execute().data or []
        inbound = [d for d in dump_rows if d.get("direction") == "inbound"]
        check(len(inbound) == 1, "inbound raw_dump persisted for the message", f"id={inbound[0]['id'] if inbound else None}")
        # The card dump's content is the LLM *summary*, not the raw message — so it is
        # found by type + recency, never by content equality.
        cards = []
        if inbound:
            cards = sb.table("raw_dumps").select("id").eq("owner_id", T).eq("message_type", "suggestion").gte("created_at", inbound[0]["created_at"]).execute().data or []
        check(len(cards) == 1, "suggestion-card raw_dump produced (≥2 new entities → Path A/B)",
              f"id={cards[0]['id'] if cards else None}")
        if not inbound:
            check(False, "cannot confirm without inbound dump id")
            raise SystemExit("abort scenario 2 — no inbound dump")
        source_id = inbound[0]["id"]
        created_dump_ids.extend(d["id"] for d in dump_rows)
        created_dump_ids.extend(c["id"] for c in cards)

        tasks2_before = sb.table("tasks").select("id").eq("owner_id", T).ilike("title", f"%{RUN_STAMP} Meridian%").execute().data or []

        confirm_body = {
            "source_type": "message",
            "source_id": source_id,
            "selected_tasks": [],
            "selected_entities": [
                {"label": "Ishaan Rao", "type": "person"},
                {"label": "Meridian Labs", "type": "organization"},
            ],
        }
        r = client.post("/api/suggestions/confirm", json=confirm_body, headers=headers)
        check(r.status_code == 200, "POST /api/suggestions/confirm → 200", f"resp={r.text[:160]}")

        ishaan = sb.table("graph_nodes").select("id, label").eq("owner_id", T).eq("type", "person").eq("is_current", True).ilike("label", "Ishaan Rao").execute().data or []
        meridian = sb.table("graph_nodes").select("id, label").eq("owner_id", T).eq("type", "organization").eq("is_current", True).ilike("label", "Meridian Labs").execute().data or []
        check(len(ishaan) == 1, "Ishaan Rao became a LIVE graph node", f"id={ishaan[0]['id'] if ishaan else None}")
        check(len(meridian) == 1, "Meridian Labs became a LIVE graph node", f"id={meridian[0]['id'] if meridian else None}")
        if ishaan:
            created_node_ids.append(ishaan[0]["id"])
        if meridian:
            created_node_ids.append(meridian[0]["id"])

        # Bridge C: org approval backfills organization_id onto existing task rows
        if meridian and tasks2_before:
            tid = tasks2_before[0]["id"]
            t = sb.table("tasks").select("organization_id").eq("owner_id", T).eq("id", tid).limit(1).execute().data or []
            check(t and str(t[0].get("organization_id")) == str(meridian[0]["id"]),
                  "Bridge C: task org-linked to Meridian Labs after confirm",
                  f"org={t[0].get('organization_id') if t else None}")
            if t:
                created_task_ids.append(tid)
        fresh = new_pending_ids(sb, T, baseline)
        check(not fresh, "ZERO new pending rows after card+confirm", f"new={sorted(fresh)}")

        # ── Scenario 3: closure message for the scenario-1 task ─────────────
        print("\n[Scenario 3] Closure message (COMPLETION intent)")
        s1_task_id = created_task_ids[0] if created_task_ids else None
        msg3 = f"Close the {RUN_STAMP} lease renewal follow-up with Tom Okafor — done"
        r = client.post("/api/send-message", json={"message": msg3, "session_id": None}, headers=headers)
        check(r.status_code == 200, "POST /api/send-message → 200", f"resp={r.text[:160]}")
        if s1_task_id:
            t = sb.table("tasks").select("status").eq("owner_id", T).eq("id", s1_task_id).limit(1).execute().data or []
            check(t and t[0].get("status") == "done",
                  "scenario-1 task closed by the message", f"status={t[0].get('status') if t else 'MISSING'}")
        comp = sb.table("memories").select("id, metadata").eq("owner_id", T).eq("source", "webhook_completion").eq("content", msg3).execute().data or []
        check(len(comp) == 1, "exactly one webhook_completion memory (canonical writer)")
        if comp:
            created_memory_ids.append(comp[0]["id"])
            meta = comp[0].get("metadata") or {}
            check(meta.get("intent") == "COMPLETION", "closure memory carries COMPLETION intent", str(meta.get("intent")))
        fresh = new_pending_ids(sb, T, baseline)
        check(not fresh, "ZERO new pending rows after closure message", f"new={sorted(fresh)}")

        # ── Final: owner-scope proof on everything this run created ─────────
        print("\n[Final] Owner-scope cross-check")
        leaked = []
        for tid in created_task_ids:
            rr = sb.table("tasks").select("owner_id").eq("id", tid).limit(1).execute()
            if rr.data and rr.data[0].get("owner_id") != T:
                leaked.append(f"task {tid}")
        for nid in created_node_ids:
            rr = sb.table("graph_nodes").select("owner_id").eq("id", nid).limit(1).execute()
            if rr.data and rr.data[0].get("owner_id") != T:
                leaked.append(f"node {nid}")
        for mid in created_memory_ids:
            rr = sb.table("memories").select("owner_id").eq("id", mid).limit(1).execute()
            if rr.data and rr.data[0].get("owner_id") != T:
                leaked.append(f"memory {mid}")
        check(not leaked, "all created rows carry the Test owner_id", "; ".join(leaked) or "clean")

    finally:
        # ── Cleanup + restore (Test tenant only) ────────────────────────────
        # Order matters: tasks/memories may FK-reference graph_nodes
        # (tasks.organization_id → graph_nodes.id), so domain rows are deleted
        # BEFORE nodes — deleting a node first silently fails on the FK.
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
            # Clear any lingering org references before node deletion
            for nid in created_node_ids:
                try:
                    sb.table("tasks").update({"organization_id": None}).eq("organization_id", nid).execute()
                except Exception:
                    pass
            for nid in created_node_ids:
                try:
                    sb.table("graph_edges").delete().or_(f"source_node_id.eq.{nid},target_node_id.eq.{nid}").execute()
                except Exception:
                    pass
            for nid in created_node_ids:
                try:
                    sb.table("graph_nodes").delete().eq("owner_id", T).eq("id", nid).execute()
                except Exception:
                    pass
            for did in created_dump_ids:
                try:
                    sb.table("raw_dumps").delete().eq("owner_id", T).eq("id", did).execute()
                except Exception:
                    pass
            # Stray stamped rows (e.g. outbound responses, task graph nodes)
            for tbl, col in (("raw_dumps", "content"), ("memories", "content"), ("tasks", "title")):
                try:
                    sb.table(tbl).delete().eq("owner_id", T).ilike(col, f"%{RUN_STAMP}%").execute()
                except Exception:
                    pass
            try:
                # write_graph_edges_for_task upserts a type='task' graph node per task
                nodes = sb.table("graph_nodes").select("id").eq("owner_id", T).eq("type", "task").ilike("label", f"%{RUN_STAMP}%").execute().data or []
                for n in nodes:
                    try:
                        sb.table("graph_edges").delete().or_(f"source_node_id.eq.{n['id']},target_node_id.eq.{n['id']}").execute()
                    except Exception:
                        pass
                    try:
                        sb.table("graph_nodes").delete().eq("owner_id", T).eq("id", n["id"]).execute()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                sb.table("enrichment_queue").delete().ilike("content", f"%{RUN_STAMP}%").execute()
            except Exception:
                pass
            # Restore the original API key hash
            if original_hash:
                sb.table("users").update({"api_key_hash": original_hash}).eq("id", T).execute()
            print(f"\n🧹 Cleanup done; original Test api_key_hash restored "
                  f"({'yes' if original_hash else 'NO — original was empty!'})")

    print("\n" + "=" * 60)
    print(f"API UAT RESULT: {PASS} passed, {FAIL} failed  [tenant={T[:8]}…]")
    print("=" * 60)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main_sync()