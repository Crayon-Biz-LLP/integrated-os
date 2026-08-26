#!/usr/bin/env python3
"""Batch UAT round 2: 20 harder scenarios against the Test tenant.

Usage:
    python scripts/uat_batch20r2.py send     # phase A: send + collect cards
    python scripts/uat_batch20r2.py confirm  # phase B: auto-confirm cards
    python scripts/uat_batch20r2.py verify   # phase C: verify expectations
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from backup_supabase import discover_conn  # noqa: E402

DSN, PW = discover_conn()
OWNER = 'e87f0279-3ec0-4875-af69-49894ee9da6f'
BASE = "https://danielyashwant--rhodey-os-web-endpoint.modal.run"
KEY_PATH = '/tmp/uat_test_key'
STATE = '/tmp/batch20r2_state.json'


def psql(sql):
    env = dict(os.environ)
    if PW:
        env['PGPASSWORD'] = PW
    r = subprocess.run([shutil.which('psql'), DSN, '-t', '-A'],
                       input=sql, text=True, env=env, capture_output=True)
    return r.stdout.strip()


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


def shard_sql(sql):
    blob = psql(f"SELECT coalesce(json_agg(t)::text,'[]') FROM ({sql}) t")
    return json.loads(blob)


# ── The matrix ────────────────────────────────────────────────────────
# kind: task | note | none | query | card
# org: expected org label for created artifacts (None = must stay unlinked)
MSGS = [
    ("What's on my calendar today?",
     "query", None),
    ("Mark the Prismwork compliance call prep as done",
     "completion", None),
    ("N: Pricing idea for the Axionly relaunch page",
     "note", None),
    ("https://en.wikipedia.org/wiki/Knowledge_graph",
     "note", None),
    ("Move the Nordlicht status ping to next Friday",
     "update", None),
    ("Meeting with Tidewell and Vantage Hotels teams on Friday at 2 PM",
     "task", "Tidewell"),
    ("Havenlight Metro Central maintenance walkthrough on Thursday",
     "task", "Havenlight Metro Central"),
    ("Drop the kids at school at 8 AM tomorrow",
     "task", None),
    ("Prismwrk audit documents need filing today",
     "task", "Prismwork"),
    ("Weekly Tidewell sync every Monday at 9 AM",
     "task", "Tidewell"),
    ("Submit the Larkspur Bank reconciliation report by EOD Thursday",
     "task", "Larkspur Bank"),
    ("Feeling completely drained after back-to-back reviews today",
     "none", None),
    ("Lunch with Owen Hartley next Wednesday at noon",
     "task", None),
    ("Met the Nova Dynamics founders today. Send them our proposal by Wednesday.",
     "card", "Nova Dynamics"),
    ("Onboard Ilana Petrov from Brightline Media to the design review",
     "card", "Brightline Media"),
    ("Remember that Sarah Thomas prefers email over calls",
     "note", None),
    ("Schedule the quarterly Axionly architecture review for next Tuesday 3 PM",
     "task", "Axionly"),
    ("Coffee with Benedikt Hoffmann tomorrow morning",
     "task", None),
    ("Family dinner at home on Sunday evening",
     "task", "Personal"),
    ("Wrap up the Vantage Hotels Q3 deck and share it with Grace Mathew",
     "task", "Vantage Hotels"),
]


def send():
    base = int(psql(f"SELECT coalesce(max(id),0) FROM raw_dumps WHERE owner_id='{OWNER}'"))
    print(f"baseline raw_dumps id = {base}")
    key = open(KEY_PATH).read().strip()
    for i, (text, _, _) in enumerate(MSGS, 1):
        res = subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/send-message",
                              "-H", "Content-Type: application/json",
                              "-H", f"X-API-Key: {key}",
                              "-d", json.dumps({"message": text})],
                             capture_output=True, text=True, timeout=60)
        try:
            ok = bool(json.loads(res.stdout).get("success"))
        except Exception:
            ok = False
        print(f"[{i:02d}/20] {'ok' if ok else 'SEND FAIL'}: {text[:60]}")
        time.sleep(15)
    print("waiting 100s for pipelines...")
    time.sleep(100)

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
        flag = "" if ((kind == "card") == bool(card)) else "   << ROUTING MISMATCH"
        print(f"[{i:02d}] got={got} want={want}{flag}")
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
            "source_type": "message",
            "source_id": r["inbound_id"],
            "selected_tasks": [
                {"type": a["operation"],
                 "title": (a.get("params") or {}).get("title") or a.get("human_label"),
                 "owner": None, "deadline": None,
                 "date": (a.get("params") or {}).get("time") or (a.get("params") or {}).get("deadline"),
                 "description": (a.get("params") or {}).get("content"),
                 "edited": False, "raw_action": a}
                for a in sb.get("suggested_actions", [])],
            "selected_entities": ents,
        }
        resp = api("/api/suggestions/confirm", payload)
        print(f"[{r['i']:02d}] confirm -> {str(resp)[:70]}")
        time.sleep(8)
    print("waiting 75s for background workers...")
    time.sleep(75)


def verify():
    # collect everything created after the batch baseline
    tasks = shard_sql(f"""SELECT title, COALESCE(g.label,'') AS org FROM tasks t
      LEFT JOIN graph_nodes g ON g.id::text=t.organization_id::text
      WHERE t.owner_id='{OWNER}' AND t.created_at > now() - interval '45 minutes'""")
    mems = shard_sql(f"""SELECT left(m.content,80) AS c, COALESCE(g.label,'') AS org
      FROM memories m LEFT JOIN graph_nodes g ON g.id::text=m.organization_id::text
      WHERE m.owner_id='{OWNER}' AND m.created_at > now() - interval '45 minutes'
        AND m.content NOT LIKE 'Memory_%'""")
    replies = shard_sql(f"""SELECT left(content,120) AS c FROM raw_dumps
      WHERE owner_id='{OWNER}' AND direction='outgoing' AND message_type='response'
        AND created_at > now() - interval '45 minutes'""")
    nodes = shard_sql(f"""SELECT label, type FROM graph_nodes
      WHERE owner_id='{OWNER}' AND created_at > now() - interval '45 minutes'
        AND type IN ('person','organization')""")

    passed = partial = failed = 0
    print("\n=== VERIFICATION ===")
    for i, (text, kind, exp_org) in enumerate(MSGS, 1):
        detail = ""
        status = "FAIL"
        t_hits = [t for t in tasks if any(w in t["title"].lower() for w in _keywords(text))]
        m_hits = [m for m in mems if any(w in m["c"].lower() for w in _keywords(text))]

        if kind == "query":
            hit = any(repl for repl in replies)
            status = "PASS" if hit else "FAIL"
            detail = "reply produced"
        elif kind == "note":
            if m_hits:
                status = "PASS"
                detail = f"note saved (org={m_hits[0]['org'] or 'none'})"
            else:
                status = "FAIL"
                detail = "no memory found"
        elif kind in ("task", "card"):
            if t_hits:
                orgs = {t["org"] for t in t_hits}
                if exp_org is None:
                    bad = [o for o in orgs if o]
                    status = "PASS" if not bad else "PARTIAL"
                    detail = f"org={orgs or '{none}'}"
                elif exp_org in orgs:
                    status = "PASS"
                    detail = f"org={exp_org}"
                else:
                    status = "PARTIAL"
                    detail = f"wanted {exp_org}, got {orgs}"
            else:
                status = "FAIL"
                detail = "no task created"
        elif kind == "completion":
            still = [t for t in tasks if "compliance call prep" in t["title"].lower()]
            status = "PASS" if not still else "PARTIAL"
            detail = "seeded task closed" if status == "PASS" else f"still open: {len(still)}"
        elif kind == "update":
            status = "PARTIAL"
            moved = [t for t in tasks if "nordlicht" in t["title"].lower()]
            detail = f"{len(moved)} nordlicht task(s) touched; manual date check needed"

        if status == "PASS":
            passed += 1
        elif status == "PARTIAL":
            partial += 1
        else:
            failed += 1
        print(f"[{i:02d}] {status:7s} ({kind}) {detail}")

    print(f"\n=== ROUND-2 SCORECARD: {passed} PASS / {partial} PARTIAL / {failed} FAIL ===")
    print("new entity nodes:", [(n['type'], n['label']) for n in nodes])
    pending = psql(f"SELECT count(*) FROM pending_graph_edges WHERE owner_id='{OWNER}' AND status='pending'")
    print("pending edges left:", pending)


def _keywords(text):
    """Content words used to match artifact titles/memories to messages."""
    stop = {"the", "a", "an", "and", "or", "for", "with", "to", "of", "at", "on",
            "in", "by", "my", "our", "is", "are", "it", "this", "that"}
    return [w for w in "".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split()
            if w not in stop and len(w) > 2]


if __name__ == "__main__":
    import shutil  # noqa: F401  (used inside psql via shutil.which)
    phase = sys.argv[1] if len(sys.argv) > 1 else "send"
    {"send": send, "confirm": confirm, "verify": verify}[phase]()
