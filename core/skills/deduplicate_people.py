#!/usr/bin/env python3
"""Deduplicate People - Merge duplicate entries and clean up generic/non-person records.

Three categories:
1. Duplicates: Same person from different sources with varying name formats
2. Generic/divine: Non-person entries extracted from text (God, Jesus, Lord, etc.)
3. Role labels: Entries that are roles not individuals (Wife, Customer, etc.)

Usage:
    python core/skills/deduplicate_people.py [--dry-run]

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars.
Uses --dry-run flag (like cleanup_orphans.py) for safe preview.
"""
import os
import re
import sys
import json
import difflib

from core.services.db import get_supabase

supabase = get_supabase()

# ─── Configuration ───────────────────────────────────────────────────────────

# Explicit merge groups: survivor record absorbs duplicates
# Survivor is the record to keep (prefer one with role/more info/older)
KNOWN_MERGE_GROUPS = [
    {
        "survivor_id": 1,
        "duplicate_ids": [34, 76, 69],
        "label": "Sunju / Sunjula Daniel / Sunjula Daniel (Ashraya) / Sunjula",
        "rename": "Sunjula Daniel",
    },
    {
        "survivor_id": 13,
        "duplicate_ids": [38, 65],
        "label": "Marcus / Marcus Durai / Pastor Marcus",
    },
    {
        "survivor_id": 48,
        "duplicate_ids": [61],
        "label": "Sisters family / Sister's family",
    },
    {
        "survivor_id": 30,
        "duplicate_ids": [5],
        "label": "Anita / Anita Hariharan",
    },
    {
        "survivor_id": 67,
        "duplicate_ids": [27],
        "label": "Timmy / Timmy the Auditor",
    },
    {
        "survivor_id": 35,
        "duplicate_ids": [75],
        "label": "Binu Varghese / Binu",
    },
    {
        "survivor_id": 43,
        "duplicate_ids": [54],
        "label": "Jesus / Jesus Christ",
    },
    {
        "survivor_id": 41,
        "duplicate_ids": [42],
        "label": "Father / Heavenly Father",
    },
    {
        "survivor_id": 1,
        "duplicate_ids": [44],
        "label": "Sunjula Daniel / Wife",
    },
]

# Entries to delete entirely (people + their graph_nodes)
DELETE_ENTRIES = [
    52,  # Kids (generic — the actual kids are Jeremy, Jaden, Jeffery)
]

GENERIC_LABELS = {
    # Divine and spiritual entities
    "god", "father", "heavenly father", "jesus", "lord", "the devil",
    "jesus christ", "the elders",
    # Role and generic labels
    "wife", "parents", "sister's family", "sisters family", "customer",
    "employee", "finance manager", "kids", "shirley", "author", "narrator",
    "user", "mother", "aunt", "uncle",
}

# Titles stripped during name normalization for matching
TITLES = ["pastor ", "dr. ", "dr ", "mr. ", "mr ", "mrs. ", "mrs ",
          "ms. ", "ms ", "rev. ", "rev ", "fr. ", "fr ", "saint "]

# ─── Helpers ─────────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r'\(.*?\)', '', name).strip()
    for title in TITLES:
        if name.startswith(title):
            name = name[len(title):]
            break
    return name.strip()


def is_generic(name: str) -> bool:
    return name.lower().strip() in GENERIC_LABELS


def fetch_all_paginated(table: str, select: str = "*",
                        in_filter_col: str = None,
                        in_filter_val: list = None) -> list:
    rows = []
    start = 0
    page_size = 1000
    while True:
        query = supabase.table(table).select(select)
        if in_filter_col and in_filter_val:
            query = query.in_(in_filter_col, in_filter_val)
        res = query.range(start, start + page_size - 1).execute()
        data = res.data or []
        rows.extend(data)
        if len(data) < page_size:
            break
        start += page_size
    return rows


def safe_meta(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def update_graph_node_people_id(node_id: int, people_id: int):
    res = supabase.table("graph_nodes").select("metadata").eq("id", node_id).maybe_single().execute()
    if not res or not res.data:
        return
    meta = safe_meta(res.data.get("metadata"))
    meta["people_id"] = people_id
    supabase.table("graph_nodes").update({"metadata": json.dumps(meta)}).eq("id", node_id).execute()


def update_email_linked_person(email_id: int, people_id: int):
    supabase.table("emails").update({"linked_person_id": people_id}).eq("id", email_id).execute()


def update_memory_people_id(memory_id: int, people_id: int):
    res = supabase.table("memories").select("metadata").eq("id", memory_id).maybe_single().execute()
    if not res or not res.data:
        return
    meta = safe_meta(res.data.get("metadata"))
    meta["people_id"] = people_id
    supabase.table("memories").update({"metadata": json.dumps(meta)}).eq("id", memory_id).execute()


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print(f"{'='*60}")
    print(f"  People Deduplication — {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}")

    # 1. Fetch all data
    print("\n  Fetching all people...")
    all_people = fetch_all_paginated("people", "id, name, role, strategic_weight, source, created_at")
    all_people.sort(key=lambda p: str(p.get("created_at", "")))
    people_by_id = {p["id"]: p for p in all_people}
    print(f"    Found {len(all_people)} people records")

    print("\n  Fetching references...")
    graph_node_refs = fetch_all_paginated("graph_nodes", "id, metadata",
                                          in_filter_col="type", in_filter_val=["person"])
    email_refs = fetch_all_paginated("emails", "id, linked_person_id")
    memory_refs = fetch_all_paginated("memories", "id, metadata")
    print(f"    {len(graph_node_refs)} person-type graph_nodes")
    print(f"    {len(email_refs)} emails")
    print(f"    {len(memory_refs)} memories")

    # Build reference maps: people_id → list of referencing record IDs
    person_gn: dict[int, list[int]] = {}
    person_em: dict[int, list[int]] = {}
    person_mem: dict[int, list[int]] = {}

    for gn in graph_node_refs:
        pid = safe_meta(gn.get("metadata")).get("people_id")
        if pid:
            person_gn.setdefault(int(pid), []).append(gn["id"])
    for em in email_refs:
        pid = em.get("linked_person_id")
        if pid:
            person_em.setdefault(int(pid), []).append(em["id"])
    for mem in memory_refs:
        pid = safe_meta(mem.get("metadata")).get("people_id")
        if pid:
            person_mem.setdefault(int(pid), []).append(mem["id"])

    def ref_count(pid: int) -> int:
        return (len(person_gn.get(pid, []))
                + len(person_em.get(pid, []))
                + len(person_mem.get(pid, [])))

    # Track IDs that will be deleted (so we skip them in later steps)
    deleted_ids: set[int] = set()

    # 2. Process known merge groups
    print_section("Processing known merge groups")
    total_merged = 0
    total_refs_updated = 0

    for group in KNOWN_MERGE_GROUPS:
        sid = group["survivor_id"]
        dups = group["duplicate_ids"]
        label = group["label"]

        if sid not in people_by_id:
            print(f"    Skipping '{label}' — survivor id={sid} not found")
            continue

        if sid in deleted_ids:
            print(f"    Skipping '{label}' — survivor id={sid} already merged out")
            continue

        survivor = people_by_id[sid]
        valid_dups = [d for d in dups if d in people_by_id and d not in deleted_ids]
        if not valid_dups:
            print(f"    {label} — no remaining duplicates to merge")
            continue

        print(f"\n    Group: {label}")
        print(f"      KEEP:  id={survivor['id']:>3} '{survivor['name']:<30}' "
              f"role='{survivor.get('role') or ''}' weight={survivor.get('strategic_weight')}")

        for d in valid_dups:
            dup = people_by_id[d]
            gn = len(person_gn.get(d, []))
            em = len(person_em.get(d, []))
            mem = len(person_mem.get(d, []))
            refs = f"{gn}gn {em}em {mem}mem" if (gn + em + mem) > 0 else "no refs"
            print(f"      MERGE: id={dup['id']:>3} '{dup['name']:<30}'  ({refs})")

        if dry_run:
            continue

        for d in valid_dups:
            for gn_id in person_gn.get(d, []):
                update_graph_node_people_id(gn_id, sid)
                total_refs_updated += 1
            for em_id in person_em.get(d, []):
                update_email_linked_person(em_id, sid)
                total_refs_updated += 1
            for mem_id in person_mem.get(d, []):
                update_memory_people_id(mem_id, sid)
                total_refs_updated += 1
            supabase.table("people").delete().eq("id", d).execute()
            print(f"      -> Merged id={d} ('{people_by_id[d]['name']}') into id={sid}")
            deleted_ids.add(d)
            total_merged += 1

    # 3. Auto-detect additional duplicates (normalized name + fuzzy)
    print_section("Auto-detecting additional duplicates")

    remaining = [p for p in all_people if p["id"] not in deleted_ids]
    norm_map: dict[str, list[dict]] = {}
    for p in remaining:
        norm = normalize_name(p["name"])
        if norm:
            norm_map.setdefault(norm, []).append(p)

    extra_groups_found = False
    for norm, group in sorted(norm_map.items()):
        if len(group) < 2:
            continue
        extra_groups_found = True
        print(f"\n    Same normalized name '{norm}':")
        for p in group:
            refs = ref_count(p["id"])
            print(f"      id={p['id']:>3} '{p['name']:<30}'  ({refs} refs)")

    # Substring matching for remaining (e.g. "Sunju" vs "Sunjula Daniel")
    checked: set[int] = set()
    for p in remaining:
        if p["id"] in checked:
            continue
        base_norm = normalize_name(p["name"])
        if not base_norm or len(base_norm) < 3:
            continue
        matches = []
        for q in remaining:
            if q["id"] == p["id"] or q["id"] in checked:
                continue
            q_norm = normalize_name(q["name"])
            if not q_norm:
                continue
            if base_norm in q_norm or q_norm in base_norm:
                matches.append(q)
            else:
                ratio = difflib.SequenceMatcher(None, base_norm, q_norm).ratio()
                if ratio >= 0.7:
                    matches.append(q)
        if matches:
            extra_groups_found = True
            print(f"\n    Possible duplicate for '{p['name']}' (id={p['id']}):")
            for m in matches:
                print(f"      id={m['id']:>3} '{m['name']}'  "
                      f"(sim: {difflib.SequenceMatcher(None, normalize_name(p['name']), normalize_name(m['name'])).ratio():.0%})")
            checked.add(p["id"])
            checked.update(m["id"] for m in matches)

    if not extra_groups_found:
        print("    No additional duplicates detected beyond known groups")

    # 3b. Delete entries with their graph_nodes
    if DELETE_ENTRIES:
        print_section("Deleting specified entries + their graph nodes")
        for pid in DELETE_ENTRIES:
            if pid not in people_by_id or pid in deleted_ids:
                print(f"    Skipping id={pid} — not found or already deleted")
                continue
            p = people_by_id[pid]
            gn_ids = person_gn.get(pid, [])
            print(f"    Deleting id={pid} '{p['name']}' ({len(gn_ids)} graph_nodes)")
            if not dry_run:
                for gn_id in gn_ids:
                    supabase.table("graph_nodes").delete().eq("id", gn_id).execute()
                supabase.table("people").delete().eq("id", pid).execute()
                deleted_ids.add(pid)
                total_merged += 1
                print(f"      -> Deleted id={pid} and {len(gn_ids)} graph_node(s)")

    # 4. Handle generic entries
    print_section("Processing generic/non-person entries")

    generic_entries = [(p, ref_count(p["id"]))
                       for p in all_people
                       if p["id"] not in deleted_ids and is_generic(p["name"])]

    if not generic_entries:
        print("    No generic entries found")
    else:
        zero_ref = [(p, r) for p, r in generic_entries if r == 0]
        has_ref = [(p, r) for p, r in generic_entries if r > 0]

        if zero_ref:
            print(f"\n    {len(zero_ref)} entries with no references (safe to delete):")
            for p, r in zero_ref:
                print(f"      id={p['id']:>3} '{p['name']:<30}' source={p.get('source')}")
            if not dry_run:
                ids = [p["id"] for p, _ in zero_ref]
                supabase.table("people").delete().in_("id", ids).execute()
                deleted_ids.update(ids)
                print(f"\n      -> Deleted {len(ids)} generic entries")
            else:
                print(f"\n      -> Would delete {len(zero_ref)} entries")

        if has_ref:
            print(f"\n    {len(has_ref)} entries WITH references — manual review needed:")
            for p, r in has_ref:
                gn = len(person_gn.get(p["id"], []))
                em = len(person_em.get(p["id"], []))
                mem = len(person_mem.get(p["id"], []))
                print(f"      id={p['id']:>3} '{p['name']:<30}'  "
                      f"({gn}gn {em}em {mem}mem) source={p.get('source')}")

    # 5. Survivor enrichment: rename, copy role/weight from merged duplicates
    if not dry_run:
        print_section("Enriching survivors with merged data")
        enriched = 0
        for group in KNOWN_MERGE_GROUPS:
            sid = group["survivor_id"]
            if sid not in people_by_id or sid in deleted_ids:
                continue
            survivor = people_by_id[sid]
            update = {}
            rename = group.get("rename")
            if rename and survivor.get("name") != rename:
                update["name"] = rename
            for d in group["duplicate_ids"]:
                if d not in people_by_id:
                    continue
                dup = people_by_id[d]
                if not survivor.get("role") and dup.get("role"):
                    update["role"] = dup["role"]
                if (survivor.get("strategic_weight") or 0) < (dup.get("strategic_weight") or 0):
                    update["strategic_weight"] = dup["strategic_weight"]
            if update:
                supabase.table("people").update(update).eq("id", sid).execute()
                print(f"    Updated id={sid} '{survivor['name']}' → {update}")
                enriched += 1
        if enriched == 0:
            print("    No survivors needed enrichment")

    # Summary
    print_section("Summary")
    print(f"  Known merge groups:           {len(KNOWN_MERGE_GROUPS)}")
    print(f"  Duplicate records merged out: {total_merged}")
    print(f"  References updated:           {total_refs_updated}")
    print(f"  Generic entries deleted:      {len([e for e in generic_entries if ref_count(e[0]['id']) == 0])}")
    if not dry_run:
        final_count = len(fetch_all_paginated("people", "id"))
        print(f"  Remaining people records:    {final_count}")

    print(f"\n{'  DONE  ' if not dry_run else '  DRY RUN COMPLETE  '}")
    if dry_run:
        print("  Run without --dry-run to execute")


if __name__ == "__main__":
    main()
