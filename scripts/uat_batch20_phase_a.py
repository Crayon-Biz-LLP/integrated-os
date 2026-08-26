#!/usr/bin/env python3
"""Batch UAT: 20 mixed rich/regular notes against the Test tenant via /api/send-message.
Phase A: send all messages (paced), collect cards produced.
Writes /tmp/batch20_state.json for phase B (auto-confirm + verify).
"""
import json
import subprocess
import sys
import shutil
import time
import os
sys.path.insert(0, 'scripts')
from backup_supabase import discover_conn

DSN, PW = discover_conn()
OWNER = 'e87f0279-3ec0-4875-af69-49894ee9da6f'
BASE = "https://danielyashwant--rhodey-os-web-endpoint.modal.run"
KEY = open('/tmp/uat_test_key').read().strip()

def psql(sql):
    env = dict(os.environ)
    if PW:
        env['PGPASSWORD'] = PW
    r = subprocess.run([shutil.which('psql'), DSN, '-t', '-A'], input=sql, text=True, env=env, capture_output=True)
    return r.stdout.strip()

# (text, expect_card, expected_org_for_created_task_or_None)
MSGS = [
    # ── Regular / direct-path (existing-or-no entities) ──
    ("Review the Q3 budget spreadsheet today", False, None),
    ("Review the accounts for Havenlight Metro East this week", False, "Havenlight Metro East"),
    ("Send the invoice follow-up for Tidewell", False, "Tidewell"),
    ("Prepare the compliance checklist for Prismwork", False, "Prismwork"),
    ("Call Priya Sharma about the contract renewal", False, None),
    ("Draft the platform brief for Axionly by Friday", False, "Axionly"),
    ("Update the reconciliation sheet for Larkspur Bank tomorrow", False, "Larkspur Bank"),
    ("We have a meeting tomorrow at 4 PM", False, None),
    ("Renew the Havenlight National insurance policy this month", False, "Havenlight National"),
    ("Quarterly review deck for Vantage Hotels due end of week", False, "Vantage Hotels"),
    # ── Rich / card-flow (contains something NEW) ──
    ("Scheduled a demo with Vertex Analytics on Thursday. Collaborating with Nordlicht.", True, "Vertex Analytics"),
    ("Meet Kavya Raman from Solstice Labs next Tuesday", True, "Solstice Labs"),
    ("Onboard Ravi Menon and Anita Desai to the Tidewell rollout", True, "Tidewell"),
    ("Coffee with Marcus Hale tomorrow", True, None),
    ("Pitch meeting with Brightline Media tomorrow 10 AM. Brief Owen Hartley first.", True, "Brightline Media"),
    ("Sync with Farhan Ali about the new Zephyr Labs partnership", True, "Zephyr Labs"),
    ("Add Daniel Whitmore to the Prismwork audit call on Wednesday", True, "Prismwork"),
    ("Lunch with Sarah Thomas at Vantage Hotels on Friday", True, "Vantage Hotels"),
    ("Family dinner at home on Saturday evening", True, "Personal"),
    ("Intro call with Grace Mathew regarding the Cobalt & Finch contract", True, None),
]

# baseline: highest raw_dumps id before we start
base = int(psql(f"SELECT coalesce(max(id),0) FROM raw_dumps WHERE owner_id='{OWNER}'"))
print(f"baseline raw_dumps id = {base}")

results = []
for i, (text, expect_card, exp_org) in enumerate(MSGS, 1):
    tag = f"[{i:02d}/20]"
    payload = json.dumps({"message": text})
    res = subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/send-message",
                          "-H", "Content-Type: application/json",
                          "-H", f"X-API-Key: {KEY}", "-d", payload],
                         capture_output=True, text=True, timeout=60)
    try:
        resp = json.loads(res.stdout)
        ok = bool(resp.get("success"))
    except Exception as e:
        ok = False
        print(f"{tag} SEND FAILED: {e} | {res.stdout[:120]}")
    print(f"{tag} sent ({'ok' if ok else 'FAIL'}): {text[:60]}")
    results.append({"i": i, "text": text, "expect_card": expect_card, "exp_org": exp_org, "sent": ok})
    time.sleep(15)   # pace: classify limiter is 15/60s

# wait for pipeline to catch up, then collect what got created
print("waiting 90s for pipelines...")
time.sleep(90)

state = psql(f"""SELECT id, message_type, content, metadata::text FROM raw_dumps
 WHERE owner_id='{OWNER}' AND id > {base} ORDER BY id""")
rows = []
for line in state.split('\n'):
    if '|' not in line:
        continue
    rid, mtype, content, meta_raw = line.split('|', 3)
    meta = {}
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = {}
    rows.append({"id": int(rid), "type": mtype, "content": content, "meta": meta})

cards = [r for r in rows if r["type"] == "suggestion"]
print("\n=== PHASE A SUMMARY ===")
print(f"messages sent: {sum(1 for r in results if r['sent'])}/20")
print(f"suggestion cards produced: {len(cards)}")

# attach card info per message (card metadata.message_id -> inbound id)
by_msg_id = {c["meta"].get("suggestion_breakdown", {}).get("message_id"): c for c in cards}
for r in results:
    inbound = next((x for x in rows if x["type"] == "text" and x["content"].startswith(r["text"][:40])), None)
    r["inbound_id"] = inbound["id"] if inbound else None
    card = by_msg_id.get(r["inbound_id"])
    r["got_card"] = card is not None
    mark = ""
    if r["expect_card"] and not r["got_card"]:
        mark = "  << EXPECTED CARD, NONE"
    if not r["expect_card"] and r["got_card"]:
        mark = "  << UNEXPECTED CARD"
    print(f"[{r['i']:02d}] card={r['got_card']}{mark}")

json.dump({"results": results,
           "cards": [{"row": c["id"], "meta": c["meta"]} for c in cards]},
          open('/tmp/batch20_state.json', 'w'))
print("state saved to /tmp/batch20_state.json")
