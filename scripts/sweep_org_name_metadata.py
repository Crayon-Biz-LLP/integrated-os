"""
One-time data sweep (Sep 2026): remove the vestigial `organization_name` key
from memories.metadata across all tenants.

Why: metadata.organization_name was a redundant second copy of org identity
that could diverge from the authoritative organization_id (Plumfleet id +
"Qhord" name). Nothing in core/api/app/frontend reads it. The hardened
create_note_direct no longer writes it; this clears the legacy rows.

Idempotent + scoped: only strips the one key; leaves every other metadata
field (organization_id, person_ids, thread_id, thread_entity_name, intent)
untouched. Run with --apply to write; default is dry-run preview.

Usage:
  python3 scripts/sweep_org_name_metadata.py          # dry-run
  python3 scripts/sweep_org_name_metadata.py --apply  # write
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from supabase import create_client  # noqa: E402


def main():
    apply = "--apply" in sys.argv
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    affected = []
    last_id = 0
    while True:
        r = sb.table("memories").select("id,owner_id,metadata").gt("id", last_id).order("id").limit(1000).execute()
        rows = r.data or []
        if not rows:
            break
        for row in rows:
            md = row.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            if isinstance(md, dict) and "organization_name" in md:
                new_md = {k: v for k, v in md.items() if k != "organization_name"}
                affected.append((row["id"], row["owner_id"], md.get("organization_name"), md.get("organization_id")))
                if apply:
                    sb.table("memories").update({"metadata": new_md}).eq("id", row["id"]).execute()
        last_id = rows[-1]["id"]
        if len(rows) < 1000:
            break

    print(f"{'APPLIED' if apply else 'DRY-RUN'} — {len(affected)} memories had metadata.organization_name")
    per_tenant = {}
    for mid, oid, name, org_id in affected:
        per_tenant[oid[:8]] = per_tenant.get(oid[:8], 0) + 1
    for k, v in sorted(per_tenant.items(), key=lambda x: -x[1]):
        print(f"  tenant {k}: {v} rows")
    print("\nSample (up to 10):")
    for mid, oid, name, org_id in affected[:10]:
        print(f"  mem {mid} | tenant {oid[:8]} | org_name={name!r} | org_id={org_id}")
    print(f"\nNote 7000 (Plumfleet/Qhord case) included: {7000 in [a[0] for a in affected]}")


if __name__ == "__main__":
    main()