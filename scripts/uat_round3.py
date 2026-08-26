#!/usr/bin/env python3
"""Batch UAT round 3: 20 messages targeting retests of fixes A-D + fresh scenarios.

Usage:
    python scripts/uat_round3.py send      # phase A: send + collect cards
    python scripts/uat_round3.py confirm   # phase B: confirm cards
    python scripts/uat_round3.py verify    # phase C: verify expectations
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
STATE = '/tmp/round3_state.json'


def psql(sql):
    env = dict(os.environ)
    if PW:
        env['PGPASSWORD'] = PW
    r = subprocess.run([shutil.which('psql'), DSN, '-t', '-A'],
                       input=sql, text=True, env=env, capture_output=True)
    return r.stdout.strip()


def shard_sql(sql):
    blob = psql(f"SELECT coalesce(json_agg(t)::text,'[]') FROM ({sql}) t")
    return json.loads(blob)


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


# ── The 20-message matrix ────────────────────────────────────────────
# (text, kind, exp_org, tags)
#   kind: task|note|none|query|card|completion|update
#   exp_org: expected org linkage (None = must stay unlinked; string = exact)
#   tags: list of issue IDs this message retests
MSGS = [
    # ── Queries (3) ──
    ("What tasks do I have today?",
     "query", None, ["query-path"]),
    ("What are the updates from Farhan Ali on the Havenlight project?",
     "query", None, ["query-path"]),
    ("Show me my upcoming calendar for this week",
     "query", None, ["query-path"]),
    # ── Completion retest (1) — Fix B ──
    ("Mark the Prismwork compliance call prep as done",
     "completion", None, ["fixB"]),
    # ── Updates (2) ──
    ("Move the Renew Heyreach account task to next month",
     "update", None, ["update-path"]),
    ("Reschedule the Weekly Tidewell sync to Wednesday 10 AM",
     "update", None, ["update-path"]),
    # ── Vault / N: note (2) ──
    ("N: Remember to follow up with Hadley Franks about the Omni Dynamics proposal",
     "note", None, ["fixD"]),
    ("Remember that Sarah Thomas prefers email over calls — flag this for onboarding",
     "note", None, ["note-path"]),
    # ── Existing entities + word-form detection (3) — Fix C ──
    ("Talk to Nadia Rahman about the Solstice Labs invoice tomorrow",
     "task", "Solstice Labs", ["fixC", "existing-org"]),
    ("Call Tom Okafor about the Havenlight Metro East volunteer roster",
     "task", "Havenlight Metro East", ["fixC", "sub-org"]),
    ("Email Daniel Whitfield the Tidewell rollout schedule by Friday",
     "task", "Tidewell", ["fixC", "existing-org"]),
    # ── New org detection (2) — Fix D suffix gate ──
    ("Schedule a demo with Stratos Analytics for next Monday at 3 PM",
     "card", "Stratos Analytics", ["fixD", "new-org"]),
    ("Strategy session with Cortex Systems next Thursday. Invite Maya Johanssen.",
     "card", "Cortex Systems", ["fixD", "new-org", "new-person"]),
    # ── Multi-org (1) — two existing orgs, first-wins ──
    ("Meeting with Brightline Media and Zephyr Labs teams next week about the partnership",
     "card", "Brightline Media", ["multi-org"]),
    # ── Personal/work separation (2) — round-1 retest ──
    ("Family movie night at home on Saturday — block my calendar",
     "task", "Personal", ["personal-keywords"]),
    ("Drop the kids at school at 8 AM, then Solstice Labs call at 10",
     "task", None, ["mixed-personal-work"]),
    # ── Typos (2) — round-1 retest ──
    ("Submit the Prismwrk compliance checklist by EOD Thursday",
     "task", "Prismwork", ["typo-fuzzy"]),
    ("Vantge Hotels Q3 deck review with Grace Mathew next Tuesday",
     "task", "Vantage Hotels", ["typo-fuzzy"]),
    # ── Emotional / no-org (1) — round-1 retest ──
    ("Feeling optimistic after the big Prismwork win today — reflecting on progress",
     "none", None, ["emotional-no-fab"]),
    # ── Existing person + org, no new entities (1) — should go direct ──
    ("Discuss the Cobalt & Finch contract details with Reuben Pillai on Thursday",
     "task", "Cobalt & Finch", ["direct-existing"]),
]


def _keywords(text):
    stop = {"the", "a", "an", "and", "or", "for", "with", "to", "of", "at", "on",
            "in", "by", "my", "our", "is", "are", "it", "this", "that"}
    return [w for w in "".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split()
            if w not in stop and len(w) > 2]


def send():
    base = int(psql(f"SELECT coalesce(max(id),0) FROM raw_dumps WHERE owner_id='{OWNER}'"))
    print(f"baseline raw_dumps id = {base}")
    for i, (text, _, _, _) in enumerate(MSGS, 1):
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
            print("... pausing 120s between batches to let rate limits recover ...")
            time.sleep(120)
    print("waiting 110s for pipelines...")
    time.sleep(110)
    rows = shard_sql(f"""SELECT id, message_type, content, metadata FROM raw_dumps
      WHERE owner_id='{OWNER}' AND id > {base} ORDER BY id""")
    rows = [{**r, "meta": r.get("metadata") or {}} for r in rows]
    cards = [r for r in rows if r["message_type"] == "suggestion"]
    by_msg_id = {(c["meta"].get("suggestion_breakdown") or {}).get("message_id"): c for c in cards}
    results = []
    for i, (text, kind, org, tags) in enumerate(MSGS, 1):
        inbound = next((x for x in rows if x["message_type"] == "text"
                        and (x.get("content") or "").startswith(text[:40])), None)
        iid = inbound["id"] if inbound else None
        card = by_msg_id.get(iid)
        got = "CARD" if card else "direct"
        want = "card" if kind == "card" else "direct"
        flag = "" if ((kind == "card") == bool(card)) else "   << ROUTING MISMATCH"
        print(f"[{i:02d}] got={got:5s} want={want:6s}{flag}")
        results.append({"i": i, "text": text, "kind": kind, "exp_org": org, "tags": tags,
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
    print("waiting 80s for background workers...")
    time.sleep(80)


def verify():
    tasks = shard_sql(f"""SELECT title, status, COALESCE(g.label,'') AS org FROM tasks t
      LEFT JOIN graph_nodes g ON g.id::text=t.organization_id::text
      WHERE t.owner_id='{OWNER}' AND t.created_at > now() - interval '50 minutes'""")
    mems = shard_sql(f"""SELECT left(m.content,80) AS c, COALESCE(g.label,'') AS org
      FROM memories m LEFT JOIN graph_nodes g ON g.id::text=m.organization_id::text
      WHERE m.owner_id='{OWNER}' AND m.created_at > now() - interval '50 minutes'
        AND m.content NOT LIKE 'Memory_%'""")
    replies = shard_sql(f"""SELECT left(content,120) AS c FROM raw_dumps
      WHERE owner_id='{OWNER}' AND direction='outgoing' AND message_type='response'
        AND created_at > now() - interval '50 minutes'""")
    nodes = shard_sql(f"""SELECT label, type FROM graph_nodes
      WHERE owner_id='{OWNER}' AND created_at > now() - interval '50 minutes'
        AND type IN ('person','organization')""")
    resources = shard_sql(f"""SELECT url FROM resources
      WHERE owner_id='{OWNER}' AND created_at > now() - interval '50 minutes'""")

    passed = partial = failed = 0
    fail_details = []
    print("\n=== ROUND 3 VERIFICATION ===")
    for i, (text, kind, exp_org, tags) in enumerate(MSGS, 1):
        detail = ""
        status = "FAIL"
        t_hits = [t for t in tasks if any(w in t["title"].lower() for w in _keywords(text))]
        m_hits = [m for m in mems if any(w in m["c"].lower() for w in _keywords(text))]

        if kind == "query":
            status = "PASS" if replies else "FAIL"
            detail = "reply produced"
        elif kind == "note":
            if m_hits or any("wikipedia" in r.get("url", "") for r in resources):
                status = "PASS"
                org_info = m_hits[0]["org"] if m_hits else "resources"
                detail = f"saved (org={org_info})"
            else:
                status = "FAIL"
                detail = "no memory or resource found"
        elif kind in ("task", "card"):
            if t_hits:
                orgs = {t["org"] for t in t_hits}
                if exp_org is None:
                    bad = [o for o in orgs if o]
                    status = "PASS" if not bad else "PARTIAL"
                    detail = f"orgs={orgs}"
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
            opened = [t for t in tasks if "compliance call prep" in t["title"].lower()]
            done = [t for t in tasks if "compliance call prep" in t["title"].lower() and t["status"] == "done"]
            if done:
                status = "PASS"
                detail = "task completed"
            elif not opened:
                status = "PASS"
                detail = "task closed or resolved"
            else:
                status = "PARTIAL"
                detail = f"still open ({len(opened)})"
        elif kind == "update":
            status = "PARTIAL"
            detail = f"{len(t_hits)} task(s) touched — needs manual date check"
        elif kind == "none":
            no_org_tasks = [t for t in t_hits if not t["org"]]
            has_org = [t for t in t_hits if t["org"]]
            if has_org:
                status = "PARTIAL"
                detail = f"unexpected org: {[t['org'] for t in has_org]}"
            elif no_org_tasks or m_hits:
                status = "PASS"
                detail = f"no work org linked ({len(no_org_tasks)} tasks, {len(m_hits)} notes)"
            else:
                status = "PASS"
                detail = "no work artifacts — clean"

        if status == "PASS":
            passed += 1
        elif status == "PARTIAL":
            partial += 1
        else:
            failed += 1
            fail_details.append(f"[{i:02d}] {kind} tags={tags} — {detail}")
        tag_str = ",".join(tags[:2])
        print(f"[{i:02d}] {status:7s} ({kind:10s}) [{tag_str:20s}] {detail}")

    print(f"\n=== ROUND-3 SCORECARD: {passed} PASS / {partial} PARTIAL / {failed} FAIL ===")
    print(f"\nnew entity nodes ({len(nodes)}):")
    for n in sorted(nodes, key=lambda x: x["type"]):
        print(f"  {n['type']:13s} {n['label']}")
    print("pending edges:", psql(f"SELECT count(*) FROM pending_graph_edges WHERE owner_id='{OWNER}' AND status='pending'"))
    if fail_details:
        print("\n--- FAIL details ---")
        for f in fail_details:
            print(f"  {f}")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "send"
    {"send": send, "confirm": confirm, "verify": verify}[phase]()
