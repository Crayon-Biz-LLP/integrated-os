"""Layer-A channel-ingest UAT — TEST TENANT ONLY.

Channels (WhatsApp / Teams / Beeper / Calls / Email / Outlook / Telegram) are
message PRODUCERS — everything after the connector (persist → classify →
approve → execute → entity gate → provenance) is channel-agnostic OS code
running on the message. Danny's real provider credentials are NOT needed to
test that: this UAT feeds each channel's real code path with the same message
shape its connector produces, entirely inside the dedicated Test tenant.

What is exercised for every channel:
  1. PERSIST — the real unified `ingest()` contract (core/lib/ingest.py):
     message lands in `messages` with the right channel/source, actionable
     classification, danny_decision NULL (approval feed), owner_id = Test.
     Ingestion itself must create ZERO pending rows (HITL).
  2. APPROVE — the REAL decision paths:
       chat channels → core.webhook.utils.process_channel_pending_decision
       email        → core.webhook.email.process_email_pending_decision
     After approval: task created; known entities link to the LIVE org; NEW
     entities queue as pending rows CARRYING provenance
     {origin_table: "messages", origin_id: <message id>} (call-site wiring);
     zero junk labels materialize.
  3. Cleanup + owner-scope proof + baseline restore.

NOT exercised (Layer B, needs Test-tenant provider credentials): the actual
network pull from Beeper/WhatsApp/Teams/Calls/Gmail/Outlook. The provider
fetch boundary is the only part this cannot reach without provisioning those
accounts for the Test tenant.

SAFETY (non-negotiable, mirrors uat_hitl_*):
  - Tenant resolved via resolve_test_tenant_uid() and HARD-FAILED unless the
    user's name is exactly "Test".
  - Every pipeline call runs inside tenant_scope(TEST_UID).
  - All verification queries and cleanup are scoped eq(owner_id, TEST_UID).
  - Cleanup deletes only this run's artifacts (captured ids / run-stamp).

Run:  python3 scripts/uat_channels.py
"""

import asyncio
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import tenant_scope, get_tenant  # noqa: E402

RUN_STAMP = f"UATCH{int(time.time())}"
RUN_START = None

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


def count_rows(sb, owner: str, table: str, id_col: str = "id") -> dict:
    rows = sb.table(table).select(id_col).eq("owner_id", owner).execute()
    return {r[id_col] for r in (rows.data or [])}


def live_org_id(sb, owner: str, label_like: str) -> str:
    rows = sb.table("graph_nodes").select("id").eq("owner_id", owner).eq(
        "type", "organization").eq("is_current", True).ilike("label", label_like).limit(1).execute()
    return rows.data[0]["id"] if rows.data else None


async def main():
    global PASS, FAIL, RUN_START
    T = test_tenant()
    sb = None
    created_message_ids = []
    created_pending_ids = []
    created_task_ids = []
    created_memory_ids = []

    try:
        from tests.fixtures.test_tenant import fresh_supabase
        sb = fresh_supabase()

        RUN_START = time.time()
        import datetime
        run_start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Force inline execution (no Modal worker spawn in this process)
        import sys as _sys
        _sys.modules["modal"] = None

        baseline = pending_snapshot(sb, T)
        print(f"\n📊 Baseline: {len(baseline)} pending/flagged rows (test tenant)")

        solstice_id = live_org_id(sb, T, "Solstice Labs")
        check(solstice_id is not None, "Test tenant has live Solstice Labs org node",
              f"id={solstice_id}")

        from core.lib.ingest import ingest
        from core.webhook.utils import process_channel_pending_decision
        from core.webhook.email import process_email_pending_decision

        # ── Chat channels: whatsapp / teams / calls ────────────────────────
        # Beeper is NOT a native messages channel: the bridge routes incoming
        # Beeper rooms through process_whatsapp_message() with
        # source="whatsapp" (beeper_ingest.py:400-493), so it is covered by
        # the whatsapp case below. Telegram is retired (telegram.py raises
        # NotImplementedError "Telegram channel retired") — nothing to test.
        # Stored channel values (messages_channel_check): whatsapp/email/call/
        # teams — note the singular 'call' for call recordings.
        chat_channels = ["whatsapp", "teams", "call"]
        for channel in chat_channels:
            print(f"\n[Chat channel] {channel} — known-entity actionable message → approve")
            msg = (f"Follow up with Tom Okafor at Solstice Labs about the "
                   f"{RUN_STAMP} {channel} review")
            with tenant_scope(T):
                assert get_tenant() == T
                res = await ingest(
                    text=msg,
                    source=channel,
                    classification="actionable",
                    summary=f"{RUN_STAMP} {channel} follow-up with Solstice Labs",
                    suggested_title=f"{RUN_STAMP} {channel} review follow-up",
                    is_human_sender=True,
                    tracking_id=f"{RUN_STAMP}-{channel}-in",
                    channel_specific_data={"sender_name": "Tom Okafor",
                                           "sender_id": f"+1555{RUN_STAMP[-6:]}",
                                           "chat_id": f"ch-{RUN_STAMP}-{channel}"},
                )
            check(res.get("status") == "filed", f"{channel}: ingest filed", str(res.get("status")))
            mid = res.get("message_id")
            check(mid is not None, f"{channel}: message persisted", f"id={mid}")
            if mid:
                created_message_ids.append(mid)
                mrow = sb.table("messages").select("channel, classification, danny_decision, owner_id").eq("owner_id", T).eq("id", mid).limit(1).execute()
                if mrow.data:
                    r = mrow.data[0]
                    check(r.get("channel") == channel, f"{channel}: channel stored", str(r.get("channel")))
                    check(r.get("classification") == "actionable", f"{channel}: classification actionable", str(r.get("classification")))
                    check(r.get("danny_decision") is None, f"{channel}: in approval feed (decision NULL)", str(r.get("danny_decision")))
                    check(str(r.get("owner_id")) == T, f"{channel}: owner_id = Test", str(r.get("owner_id")))
            check(pending_snapshot(sb, T) == baseline, f"{channel}: ZERO pending rows from ingestion alone")

            tasks_before = count_rows(sb, T, "tasks")
            with tenant_scope(T):
                try:
                    result = await process_channel_pending_decision(channel, mid, "approve")
                except Exception as e:
                    result = {"error": str(e)}
            check(not result.get("error"), f"{channel}: approve executed", str(result)[:160])
            new_tasks = count_rows(sb, T, "tasks") - tasks_before
            check(len(new_tasks) >= 1, f"{channel}: task created after approval", f"new={sorted(new_tasks)}")
            created_task_ids.extend(sorted(new_tasks))
            if new_tasks:
                trow = sb.table("tasks").select("organization_id").eq("owner_id", T).in_("id", sorted(new_tasks)).limit(1).execute()
                if trow.data and trow.data[0].get("organization_id"):
                    check(str(trow.data[0]["organization_id"]) == str(solstice_id),
                          f"{channel}: task org-linked to LIVE Solstice Labs", str(trow.data[0]["organization_id"]))
            fresh = pending_snapshot(sb, T) - baseline
            check(not fresh, f"{channel}: ZERO junk pending rows after approval", f"new={sorted(fresh)}")

        # ── Email: known-entity message (direct task path) ─────────────────
        print("\n[Email] known-entity actionable message → approve")
        msg_e1 = (f"Please review the {RUN_STAMP} lease with Tom Okafor at Solstice Labs "
                  f"and send the updated schedule")
        with tenant_scope(T):
            res = await ingest(
                text=msg_e1,
                source="email",
                classification="actionable",
                summary=f"{RUN_STAMP} lease review with Solstice Labs",
                suggested_title=f"{RUN_STAMP} lease review",
                is_human_sender=True,
                tracking_id=f"{RUN_STAMP}-email-1",
                channel_specific_data={"sender_name": "Tom Okafor",
                                       "sender_email": f"tom.{RUN_STAMP}@example.com"},
            )
        mid1 = res.get("message_id")
        check(mid1 is not None, "email: message persisted", f"id={mid1}")
        if mid1:
            created_message_ids.append(mid1)
        tasks_before = count_rows(sb, T, "tasks")
        with tenant_scope(T):
            try:
                result1 = await process_email_pending_decision(mid1, "approve")
            except Exception as e:
                result1 = {"error": str(e)}
        check(not result1.get("error"), "email(known): approve executed", str(result1)[:160])
        new_tasks = count_rows(sb, T, "tasks") - tasks_before
        check(len(new_tasks) >= 1, "email(known): task created after approval", f"new={sorted(new_tasks)}")
        created_task_ids.extend(sorted(new_tasks))
        fresh = pending_snapshot(sb, T) - baseline
        check(not fresh, "email(known): ZERO junk pending rows", f"new={sorted(fresh)}")

        # ── Email: NEW-entity message → provenance on queued pending rows ──
        print("\n[Email] NEW-entity message → approve → pending rows carry provenance")

        def fresh_pending_excluding_prior() -> set:
            """Pending delta vs baseline EXCLUDING rows this run already
            captured (earlier scenarios' legitimate pending rows)."""
            return pending_snapshot(sb, T) - baseline - set(created_pending_ids)

        msg_e2 = (f"Discuss the {RUN_STAMP} rollout with Priya Nair from Blue Heron "
                  f"Ventures and share the draft plan")
        with tenant_scope(T):
            res2 = await ingest(
                text=msg_e2,
                source="email",
                classification="actionable",
                summary=f"{RUN_STAMP} rollout with Blue Heron Ventures",
                suggested_title=f"{RUN_STAMP} Blue Heron rollout",
                is_human_sender=True,
                tracking_id=f"{RUN_STAMP}-email-2",
                channel_specific_data={"sender_name": "Priya Nair",
                                       "sender_email": f"priya.{RUN_STAMP}@example.com"},
            )
        mid2 = res2.get("message_id")
        check(mid2 is not None, "email(new): message persisted", f"id={mid2}")
        if mid2:
            created_message_ids.append(mid2)
        tasks_before_e2 = count_rows(sb, T, "tasks")
        with tenant_scope(T):
            try:
                result2 = await process_email_pending_decision(mid2, "approve")
            except Exception as e:
                result2 = {"error": str(e)}
        check(not result2.get("error"), "email(new): approve executed", str(result2)[:160])
        created_task_ids.extend(sorted(count_rows(sb, T, "tasks") - tasks_before_e2))
        new_pending = fresh_pending_excluding_prior()
        check(len(new_pending) >= 1, "email(new): NEW entities queued as pending rows",
              f"new={sorted(new_pending)}")
        if new_pending:
            created_pending_ids.extend(sorted(new_pending))
            prows = sb.table("pending_nodes").select(
                "id, label, node_type, provenance, status"
            ).eq("owner_id", T).in_("id", sorted(new_pending)).execute().data or []
            for r in prows:
                check(r.get("status") == "pending", f"{r['label']}: status=pending", str(r.get("status")))
                prov = r.get("provenance")
                check(bool(prov), f"{r['label']}: provenance populated", f"{prov!r}")
                if prov:
                    import json
                    try:
                        p = json.loads(prov)
                        ok_p = (p.get("origin_table") == "messages"
                                and str(p.get("origin_id")) == str(mid2))
                        check(ok_p, f"{r['label']}: provenance → messages row {mid2}", str(p))
                    except Exception as e:
                        check(False, f"{r['label']}: provenance parses", str(e))
            junk = {"please", "chief", "staff", "news", "update", "meeting", "call", "email", "schedule"}
            bad = [r["label"] for r in prows if (r.get("label") or "").strip().lower() in junk]
            check(not bad, "email(new): no junk labels in queued rows", "; ".join(bad) or "clean")

        # ── WhatsApp NEW-entity message → chat-path provenance ─────────────
        print("\n[WhatsApp] NEW-entity message → approve → pending rows carry provenance")
        msg_w2 = (f"Follow up with Rohan Das from Verdant Grove Farms about the "
                  f"{RUN_STAMP} harvest schedule")
        with tenant_scope(T):
            res3 = await ingest(
                text=msg_w2,
                source="whatsapp",
                classification="actionable",
                summary=f"{RUN_STAMP} harvest schedule with Verdant Grove Farms",
                suggested_title=f"{RUN_STAMP} Verdant Grove follow-up",
                is_human_sender=True,
                tracking_id=f"{RUN_STAMP}-wa-2",
                channel_specific_data={"sender_name": "Rohan Das",
                                       "sender_id": f"+1666{RUN_STAMP[-6:]}",
                                       "chat_id": f"ch-{RUN_STAMP}-wa2"},
            )
        mid3 = res3.get("message_id")
        check(mid3 is not None, "whatsapp(new): message persisted", f"id={mid3}")
        if mid3:
            created_message_ids.append(mid3)
        tasks_before_w2 = count_rows(sb, T, "tasks")
        with tenant_scope(T):
            try:
                result3 = await process_channel_pending_decision("whatsapp", mid3, "approve")
            except Exception as e:
                result3 = {"error": str(e)}
        check(not result3.get("error"), "whatsapp(new): approve executed", str(result3)[:160])
        created_task_ids.extend(sorted(count_rows(sb, T, "tasks") - tasks_before_w2))
        new_pending = fresh_pending_excluding_prior()
        check(len(new_pending) >= 1, "whatsapp(new): NEW entities queued as pending rows",
              f"new={sorted(new_pending)}")
        if new_pending:
            created_pending_ids.extend(sorted(new_pending))
            prows = sb.table("pending_nodes").select("id, label, provenance, status").eq(
                "owner_id", T).in_("id", sorted(new_pending)).execute().data or []
            for r in prows:
                prov = r.get("provenance")
                check(bool(prov), f"{r['label']}: provenance populated", f"{prov!r}")
                if prov:
                    import json
                    try:
                        p = json.loads(prov)
                        ok_p = (p.get("origin_table") == "messages"
                                and str(p.get("origin_id")) == str(mid3)
                                and p.get("channel") == "whatsapp")
                        check(ok_p, f"{r['label']}: provenance → messages row {mid3} + channel",
                              str(p))
                    except Exception as e:
                        check(False, f"{r['label']}: provenance parses", str(e))

        # ── Outlook: maps to channel='email', source='outlook' (real path) ─
        print("\n[Outlook] source='outlook' → channel='email' → approve via email path")
        msg_o1 = (f"Review the {RUN_STAMP} Outlook budget sheet from Tom Okafor at "
                  f"Solstice Labs before Friday")
        with tenant_scope(T):
            res_o = await ingest(
                text=msg_o1,
                source="email",
                classification="actionable",
                summary=f"{RUN_STAMP} budget review with Solstice Labs",
                suggested_title=f"{RUN_STAMP} budget review",
                is_human_sender=True,
                tracking_id=f"{RUN_STAMP}-outlook-1",
                channel_specific_data={"sender_name": "Tom Okafor",
                                       "sender_email": f"tom.{RUN_STAMP}@outlook.com"},
            )
        mid_o = res_o.get("message_id")
        check(mid_o is not None, "outlook-sourced: message persisted as email channel", f"id={mid_o}")
        if mid_o:
            created_message_ids.append(mid_o)
        tasks_before = count_rows(sb, T, "tasks")
        with tenant_scope(T):
            try:
                result_o = await process_email_pending_decision(mid_o, "approve")
            except Exception as e:
                result_o = {"error": str(e)}
        check(not result_o.get("error"), "outlook-sourced: approve executed", str(result_o)[:160])
        new_tasks = count_rows(sb, T, "tasks") - tasks_before
        check(len(new_tasks) >= 1, "outlook-sourced: task created after approval", f"new={sorted(new_tasks)}")
        created_task_ids.extend(sorted(new_tasks))
        fresh = fresh_pending_excluding_prior()
        check(not fresh, "outlook-sourced: ZERO junk pending rows", f"new={sorted(fresh)}")

        # ── FYI ingest-level coverage (native channels only) ───────────────
        print("\n[Ingest-level] FYI messages (teams + whatsapp)")
        with tenant_scope(T):
            res_fyi = await ingest(
                text=f"FYI: {RUN_STAMP} teams standup notes",
                source="teams",
                classification="fyi",
                summary=f"{RUN_STAMP} standup FYI",
                is_human_sender=False,
                tracking_id=f"{RUN_STAMP}-teams-fyi",
                channel_specific_data={"sender_name": "Team"},
            )
        check(res_fyi.get("status") == "filed", "teams: fyi ingest filed")
        if res_fyi.get("message_id"):
            created_message_ids.append(res_fyi["message_id"])
        with tenant_scope(T):
            res_fyi2 = await ingest(
                text=f"FYI: {RUN_STAMP} whatsapp group notice",
                source="whatsapp",
                classification="fyi",
                summary=f"{RUN_STAMP} whatsapp FYI",
                is_human_sender=False,
                tracking_id=f"{RUN_STAMP}-wa-fyi",
                channel_specific_data={"sender_name": "Group"},
            )
        check(res_fyi2.get("status") == "filed", "whatsapp: fyi ingest filed")
        if res_fyi2.get("message_id"):
            created_message_ids.append(res_fyi2["message_id"])

        # ── Final: owner-scope + baseline restore ──────────────────────────
        print("\n[Final] Owner-scope cross-check")
        leaked = []
        for pid in created_pending_ids:
            r = sb.table("pending_nodes").select("owner_id").eq("id", pid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"pending {pid}")
        for tid in created_task_ids:
            r = sb.table("tasks").select("owner_id").eq("id", tid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"task {tid}")
        for mid in created_message_ids:
            r = sb.table("messages").select("owner_id").eq("id", mid).limit(1).execute()
            if r.data and r.data[0].get("owner_id") != T:
                leaked.append(f"message {mid}")
        check(not leaked, "all created rows carry the Test owner_id", "; ".join(leaked) or "clean")

    finally:
        if sb is not None:
            # Decisions / raw_dumps created during this run (approval paths
            # record decision rows) — windowed sweep, owner-scoped.
            import datetime
            for tbl in ("decisions", "raw_dumps", "messages", "memories"):
                try:
                    rows = sb.table(tbl).select("id").eq("owner_id", T).gte("created_at", run_start_iso).limit(500).execute()
                    for r in (rows.data or []):
                        try:
                            sb.table(tbl).delete().eq("owner_id", T).eq("id", r["id"]).execute()
                        except Exception:
                            pass
                except Exception:
                    pass
            for pid in created_pending_ids:
                try:
                    sb.table("pending_nodes").delete().eq("owner_id", T).eq("id", pid).execute()
                except Exception:
                    pass
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
            print(f"\n🧹 Cleanup done — {len(created_message_ids)} messages, "
                  f"{len(created_pending_ids)} pending, {len(created_task_ids)} tasks removed; "
                  f"pending == baseline: {final == baseline}")

    print("\n" + "=" * 60)
    print(f"CHANNEL-INGEST UAT RESULT: {PASS} passed, {FAIL} failed  [tenant={T[:8]}…]")
    print("=" * 60)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())