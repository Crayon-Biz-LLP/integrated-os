"""Read-only: why are edges pending approval in Danny's tenant?

Aspect marker: graph

Answers, from live data (via Supabase REST — direct psql host has flaky DNS):
  1. How many pending_graph_edges rows exist, by status?
  2. Age distribution — old vs new (are these leftovers or fresh proposals?)
  3. approval_source — which writer created them?
  4. Newest / oldest pending edges — what kind and from when?
"""
import os
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
DANNY = "c302706e-fe61-422a-b384-68e3bc8f6f8e"
NOW = datetime.now(timezone.utc)


def fetch_all(table, owner_col="owner_id", owner=DANNY):
    """Fetch all rows for a tenant (paginated, 1000/page)."""
    rows, offset = [], 0
    while True:
        res = sb.table(table).select("*").eq(owner_col, owner) \
            .range(offset, offset + 999).execute()
        rows.extend(res.data or [])
        if len(res.data or []) < 1000:
            return rows
        offset += 1000


def age_bucket(created_at: str) -> str:
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    days = (NOW - dt).days
    if days <= 2:
        return "a. last 2 days"
    if days <= 14:
        return "b. 3-14 days"
    if days <= 60:
        return "c. 15-60 days"
    return "d. older than 60 days"


def short(dt: str, fmt="%Y-%m-%d") -> str:
    return (dt or "")[:10] if fmt == "%Y-%m-%d" else (dt or "")[5:16]


print("=== 1. pending_graph_edges by status (Danny) ===")
rows = fetch_all("pending_graph_edges")
for status, n in Counter(r["status"] for r in rows).most_common():
    print(f"  {status}: {n}")
print(f"  TOTAL: {len(rows)}")

pending = [r for r in rows if r["status"] == "pending"]
print(f"\n=== 2. Age distribution of the {len(pending)} pending ===")
for bucket, n in sorted(Counter(age_bucket(r["created_at"]) for r in pending).items()):
    print(f"  {bucket}: {n}")

print("\n=== 3. Pending by approval_source ===")
for src, n in Counter(r.get("approval_source") or "(null)" for r in pending).most_common():
    print(f"  {src}: {n}")

print("\n=== 4. Newest 12 pending ===")
for r in sorted(pending, key=lambda x: x["created_at"], reverse=True)[:12]:
    print(f"  #{r['id']} [{short(r['created_at'])}] {r.get('source_label','?')}"
          f" --{r.get('relationship','?')}--> {r.get('target_label','?')}"
          f"  (src={r.get('approval_source') or '-'})")

print("\n=== 5. Oldest 12 pending (the backlog) ===")
for r in sorted(pending, key=lambda x: x["created_at"])[:12]:
    print(f"  #{r['id']} [{short(r['created_at'])}] {r.get('source_label','?')}"
          f" --{r.get('relationship','?')}--> {r.get('target_label','?')}"
          f"  (src={r.get('approval_source') or '-'})")

print("\n=== 6. Resolved edges (throughput contrast) ===")
for status, n in Counter(r["status"] for r in rows if r["status"] != "pending").most_common():
    print(f"  {status}: {n}")
