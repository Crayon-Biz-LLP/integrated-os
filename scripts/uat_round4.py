#!/usr/bin/env python3
"""Batch UAT round 4: 20 fresh messages, 15s spacing, mid-batch pause.
Targets all fixes from this session. Usage: send / confirm / verify.
"""
import json
import os
import subprocess
import sys
import shutil
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from backup_supabase import discover_conn  # noqa: E402

DSN, PW = discover_conn()
OWNER = 'e87f0279-3ec0-4875-af69-49894ee9da6f'
BASE = "https://danielyashwant--rhodey-os-web-endpoint.modal.run"
KEY_PATH = '/tmp/uat_test_key'
STATE = '/tmp/round4_state.json'


def psql(sql):
    env = dict(os.environ)
    if PW:
        env['PGPASSWORD'] = PW
    r = subprocess.run([shutil.which('psql'), DSN, '-t', '-A'],
                       input=sql, text=True, env=env, capture_output=True)
    return r.stdout.strip()


def shard_sql(sql):
    return json.loads(psql(f"SELECT coalesce(json_agg(t)::text,'[]') FROM ({sql}) t"))


def api(path, payload):
    res = subprocess.run(["curl", "-s", "-X", "POST", BASE + path,
                          "-H", "Content-Type: application/json",
                          "-H", f"X-API-Key: {open(KEY_PATH).read().strip()}",
                          "-d", json.dumps(payload)],
                         capture_output=True, text=True, timeout=90)
    try:
        return json.loads(res.stdout)
    except Exception:
        return {"success": False, "raw": res.stdout[:150]}


MSGS = [
    ("What do I have on my plate today?",
     "query", None),
    ("Mark the Cobalt & Finch contract markup as done",
     "completion", None),
    ("N: Had a breakthrough idea for the Tidewell integration architecture",
     "note", None),
    ("Call Lisa Chen about the Havenlight volunteer roster changes",
     "task", "Havenlight"),
    ("Email Raj Iyer the Prismwork compliance checklist summary",
     "task", "Prismwork"),
    ("Discuss the Cobalt & Finch renewal with Peter Sundar next week",
     "task", "Cobalt & Finch"),
    ("New pitch: Quantum Analytics wants our Q4 platform proposal by Friday",
     "card", "Quantum Analytics"),
    ("Sync with Grace Mathew about the Axionly migration timeline",
     "task", "Axionly"),
    ("Feeling excited about the Qhord product launch next month",
     "none", None),
    ("Weekly Havenlight Metro Central check-in on Mondays at 10 AM",
     "task", "Havenlight Metro Central"),
    ("Submit the Larkspur Bank reconciliation report by Thursday EOD",
     "task", "Larkspur Bank"),
    ("Follow up with Tanvi Reddy about the Solstice Labs intro call",
     "task", "Solstice Labs"),
    ("Strategy session with Quantum Dynamics next Tuesday. Invite Nadia Rahman.",
     "card", "Quantum Dynamics"),
    ("Family budget review at home this weekend — Personal finances",
     "task", "Personal"),
    ("Prismwrk audit docs need filing by end of week",
     "task", "Prismwork"),
    ("Add Leah Verghese to the Vantage Hotels Q3 review meeting",
     "task", "Vantage Hotels"),
    ("Coffee with Marcus Webb tomorrow to discuss Nordlicht rollout",
     "task", "Nordlicht"),
    ("The Cobalt & Finch contract renewal is overdue — escalate",
     "task", "Cobalt & Finch"),
    ("Meet Tara Vasquez from Stratos Digital about the analytics dashboard",
     "card", "Stratos Digital"),
    ("Drop off dry cleaning at 4 PM, then review the Solstice Labs proposal",
     "task", None),
]


def _kw(text):
    stop = {"the", "a", "an", "and", "or", "for", "with", "to", "of", "at", "on",
            "in", "by", "my", "our", "is", "are", "it", "this", "that"}
    return [w for w in "".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split()
            if w not in stop and len(w) > 2]


def send():
    base = int(psql(f"SELECT coalesce(max(id),0) FROM raw_dumps WHERE owner_id='{OWNER}'"))
    print(f"baseline = {base}")
    for i, (text, _, _) in enumerate(MSGS, 1):
        res = subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/send-message",
                              "-H", "Content-Type: application/json",
                              "-H", f"X-API-Key: {open(KEY_PATH).read().strip()}",
                              "-d", json.dumps({"message": text})],
                             capture_output=True, text=True, timeout=60)
        try:
            ok = bool(json.loads(res.stdout).get("success"))
        except Exception:
            ok = False
        print(f"[{i:02d}/20] {'ok' if ok else 'FAIL'}: {text[:65]}")
        time.sleep(15)
        if i == 10:
            print("... pausing 120s ...")
            time.sleep(120)
    print("waiting 110s ...")
    time.sleep(110)
    rows = shard_sql(f"""SELECT id, message_type, content, metadata FROM raw_dumps
      WHERE owner_id='{OWNER}' AND id > {base} ORDER BY id""")
    rows = [{**r, "meta": r.get("metadata") or {}} for r in rows]
    cards = [r for r in rows if r["message_type"] == "suggestion"]
    by_msg_id = {(c["meta"].get("suggestion_breakdown") or {}).get("message_id"): c for c in cards}
    results = []
    for i, (text, kind, org) in enumerate(MSGS, 1):
        inbound = next((x for x in rows if x["message_type"] == "text"
                        and (x.get("content") or "").startswith(text[:40])), None)
        iid = inbound["id"] if inbound else None
        card = by_msg_id.get(iid)
        got = "CARD" if card else "direct"
        want = "card" if kind == "card" else "direct"
        flag = "" if ((kind == "card") == bool(card)) else "  << MISMATCH"
        print(f"[{i:02d}] {got:5s} want={want:6s}{flag}")
        results.append({"i": i, "text": text, "kind": kind, "exp_org": org,
                        "inbound_id": iid, "got_card": bool(card),
                        "card_meta": (card or {}).get("meta")})
    json.dump({"results": results}, open(STATE, 'w'))
    print("state saved")


def confirm():
    state = json.load(open(STATE))
    for r in state["results"]:
        if not r["got_card"]:
            continue
        sb = (r.get("card_meta") or {}).get("suggestion_breakdown", {})
        ents = []
        for e in sb.get("suggested_entities", []):
            item = {"type": e.get("type"), "label": e.get("label"), "edited": False}
            matches = e.get("existing_matches") or []
            exact = next((m for m in matches if m.get("score", 0) >= 1.2), None)
            if exact:
                item["merge_with"] = exact
            ents.append(item)
        payload = {
            "source_type": "message", "source_id": r["inbound_id"],
            "selected_tasks": [
                {"type": a["operation"],
                 "title": (a.get("params") or {}).get("title") or a.get("human_label"),
                 "owner": None, "deadline": None,
                 "date": (a.get("params") or {}).get("time") or (a.get("params") or {}).get("deadline"),
                 "description": (a.get("params") or {}).get("content"),
                 "edited": False, "raw_action": a}
                for a in sb.get("suggested_actions", [])],
            "selected_entities": ents}
        resp = api("/api/suggestions/confirm", payload)
        print(f"[{r['i']:02d}] confirm -> {str(resp)[:70]}")
        time.sleep(8)
    print("waiting 80s ...")
    time.sleep(80)


def verify():
    tasks = shard_sql(f"""SELECT title, status, COALESCE(g.label,'') AS org FROM tasks t
      LEFT JOIN graph_nodes g ON g.id::text=t.organization_id::text
      WHERE t.owner_id='{OWNER}' AND t.created_at > now() - interval '55 minutes'""")
    mems = shard_sql(f"""SELECT left(m.content,80) AS c, COALESCE(g.label,'') AS org
      FROM memories m LEFT JOIN graph_nodes g ON g.id::text=m.organization_id::text
      WHERE m.owner_id='{OWNER}' AND m.created_at > now() - interval '55 minutes'
        AND m.content NOT LIKE 'Memory_%'""")
    replies = shard_sql(f"""SELECT left(content,120) AS c FROM raw_dumps
      WHERE owner_id='{OWNER}' AND direction='outgoing' AND message_type='response'
        AND created_at > now() - interval '55 minutes'""")
    nodes = shard_sql(f"""SELECT label, type FROM graph_nodes
      WHERE owner_id='{OWNER}' AND created_at > now() - interval '55 minutes'
        AND type IN ('person','organization')""")

    passed = partial = failed = 0
    fails = []
    print("\n=== ROUND 4 VERIFICATION ===")
    for i, (text, kind, exp_org) in enumerate(MSGS, 1):
        detail = ""
        status = "FAIL"
        t_hits = [t for t in tasks if any(w in t["title"].lower() for w in _kw(text))]
        m_hits = [m for m in mems if any(w in m["c"].lower() for w in _kw(text))]

        if kind == "query":
            status = "PASS" if replies else "FAIL"
            detail = "reply"
        elif kind == "note":
            status = "PASS" if m_hits else "FAIL"
            detail = f"note ({m_hits[0]['org'] or 'none'})" if m_hits else "missing"
        elif kind in ("task", "card"):
            if t_hits:
                orgs = {t["org"] for t in t_hits}
                if exp_org is None:
                    status = "PASS" if not any(o for o in orgs) else "PARTIAL"
                    detail = f"orgs={orgs}"
                elif exp_org in orgs:
                    status = "PASS"
                    detail = f"org={exp_org}"
                else:
                    status = "PARTIAL"
                    detail = f"wanted {exp_org}, got {orgs}"
            else:
                status = "FAIL"
                detail = "no task"
        elif kind == "completion":
            done = [t for t in tasks if "contract markup" in t["title"].lower() and t.get("status") == "done"]
            opened = [t for t in tasks if "contract markup" in t["title"].lower() and t.get("status") != "done"]
            status = "PASS" if done or not opened else "PARTIAL"
            detail = f"done={len(done)} open={len(opened)}"
        elif kind == "none":
            bad = [t for t in t_hits if t["org"]]
            status = "PASS" if not bad else "PARTIAL"
            detail = f"orgs={[t['org'] for t in bad]}" if bad else "clean"

        if status == "PASS":
            passed += 1
        elif status == "PARTIAL":
            partial += 1
        else:
            failed += 1
            fails.append(f"[{i:02d}] {kind}: {detail}")
        print(f"[{i:02d}] {status:7s} ({kind:10s}) {detail}")

    print(f"\n=== ROUND-4 SCORECARD: {passed} PASS / {partial} PARTIAL / {failed} FAIL ===")
    print("new nodes:", [(n["type"], n["label"]) for n in nodes])
    print("pending edges:", psql(f"SELECT count(*) FROM pending_graph_edges WHERE owner_id='{OWNER}' AND status='pending'"))
    if fails:
        print("\n--- FAIL details ---")
        for f in fails:
            print(f"  {f}")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "send"
    {"send": send, "confirm": confirm, "verify": verify}[phase]()
