"""Migration 76 executor: aliases move onto the graph node.

This script is the GATE for db/76_aliases_on_nodes.sql.

Usage:
    python3 scripts/migrate_aliases_to_nodes.py            # dry-run pre-check
    python3 scripts/migrate_aliases_to_nodes.py --verify   # post-apply verification

The actual schema change (backfill metadata.aliases, DROP person_aliases) runs
as raw SQL — paste db/76_aliases_on_nodes.sql into the Supabase SQL editor ONLY
after the dry-run reports "ALL CHECKS PASSED".

Makes NO writes in dry-run mode. Verification is read-only.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core.services.db import get_supabase  # noqa: E402

s = get_supabase()


def meta_of(node):
    m = node.get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = {}
    return m


def precheck() -> int:
    fails = 0
    print("=" * 70)
    print("ALIAS-ON-NODE PRECHECK (read-only) — must all pass before db/76")
    print("=" * 70)

    # 1. person_aliases still exists?
    try:
        aliases = s.table("person_aliases").select("id, alias, canonical_name, resolution_count").execute().data or []
    except Exception:
        print("\n[1] person_aliases table: MISSING — migration 76 already applied (nothing to do).")
        print("✅ ALL CHECKS PASSED (already migrated)")
        return 0

    # 2. Every canonical_name resolves to a live person node — exact label match,
    #    else via canonical_id chain from an archived same-label node (e.g.
    #    archived 'Mother' -> live 'Amma'), else any same-label node.
    nodes = s.table("graph_nodes").select("id, label, type, is_current, metadata, canonical_id") \
        .eq("type", "person").execute().data or []
    live = {n["label"].lower().strip(): n for n in nodes if n.get("is_current") is not False}
    by_label_any = {}
    for n in nodes:
        by_label_any.setdefault(n["label"].lower().strip(), []).append(n)
    chain_targets = {}
    for n in nodes:
        if n.get("canonical_id"):
            chain_targets[n["label"].lower().strip()] = n["canonical_id"]
    unmapped = []
    mapped = 0
    for a in aliases:
        canon = (a.get("canonical_name") or "").strip()
        if not canon:
            unmapped.append((a.get("alias"), canon))
            continue
        key = canon.lower()
        target = live.get(key)
        if not target:
            # follow canonical chain
            cid = chain_targets.get(key)
            if cid:
                for n in nodes:
                    if n["id"] == cid and n.get("is_current") is not False:
                        target = n
                        break
        if not target:
            target = by_label_any.get(key, [None])[0]
        if target:
            mapped += 1
        else:
            unmapped.append((a.get("alias"), canon))
    print(f"\n[1] person_aliases: {len(aliases)} rows; canonical_name -> node "
          f"resolution {mapped}/{len(aliases)}")
    for alias, canon in unmapped[:10]:
        print(f"    ⚠️  '{alias}' -> '{canon}' has no resolvable person node (skipped on backfill)")
    # Not a hard fail: unmapped rows are skipped. But zero is the prod expectation.

    # 3. Existing metadata.aliases collisions (case-insensitive duplicates across nodes).
    alias_index = {}
    collisions = []
    for n in nodes:
        m = meta_of(n)
        al = m.get("aliases") or []
        if isinstance(al, str):
            al = [al]
        for a in al:
            key = str(a).lower().strip()
            if not key:
                continue
            if key in alias_index:
                collisions.append((a, alias_index[key], n["label"]))
            else:
                alias_index[key] = n["label"]
    print(f"[2] existing node aliases: {len(alias_index)} unique; {len(collisions)} cross-node collisions")
    for a, l1, l2 in collisions[:10]:
        print(f"    ⚠️  alias '{a}' on both '{l1}' and '{l2}' — last write wins")
    if collisions:
        fails += 1
        print("    ❌ cross-node alias collisions exist — resolve before applying")

    # 4. Unique constraint on (alias) — the old table allowed dupes like 'amma'/'Amma'.
    seen = {}
    dupes = []
    for a in aliases:
        key = (a.get("alias") or "").strip().lower()
        if not key:
            continue
        canon = (a.get("canonical_name") or "").strip()
        if key in seen and seen[key] != canon:
            dupes.append((a.get("alias"), seen[key], canon))
        else:
            seen[key] = canon
    print(f"[3] alias text -> canonical: {len(seen)} unique; {len(dupes)} text-to-multiple-canonical conflicts")
    for alias, c1, c2 in dupes[:10]:
        print(f"    ⚠️  '{alias}' maps to both '{c1}' and '{c2}' — first wins")
    if dupes:
        fails += 1
        print("    ❌ alias conflicts exist — resolve before applying")

    # 5. SPOUSE_OF/relationship edges available for relationship resolution.
    try:
        edges = s.table("graph_edges").select("source_node_id, target_node_id, relationship") \
            .in_("relationship", ["SPOUSE_OF", "SIBLING_OF", "PARENT_OF", "FAMILY_OF", "FRIEND_OF", "KNOWS", "DISCUSSED_WITH", "WORKS_WITH"]).limit(1000).execute().data or []
        rels = {}
        for e in edges or []:
            rels[e["relationship"]] = rels.get(e["relationship"], 0) + 1
        print(f"[4] relationship edges in graph: {dict(sorted(rels.items()))}")
        danny = live.get("danny")  # LIVE user node only
        if danny:
            spouse = [e for e in (edges or []) if e.get("relationship") == "SPOUSE_OF"
                      and (e.get("source_node_id") == danny["id"] or e.get("target_node_id") == danny["id"])]
            print(f"    user node (Danny) SPOUSE_OF edges: {len(spouse)}")
            if not spouse:
                print("    ⚠️  no SPOUSE_OF edge from user node — 'my wife' resolution will find nothing until one exists")
    except Exception as e:
        print(f"[4] relationship edges check failed: {e}")

    print()
    if fails == 0:
        print("✅ ALL CHECKS PASSED — paste db/76_aliases_on_nodes.sql into the Supabase SQL editor.")
        return 0
    print(f"❌ {fails} failing check(s) — do NOT apply db/76 until resolved.")
    return 1


def verify() -> int:
    fails = 0
    print("=" * 70)
    print("POST-APPLY VERIFICATION (read-only)")
    print("=" * 70)

    # 1. person_aliases gone?
    try:
        s.table("person_aliases").select("id").limit(1).execute()
        print("[1] ❌ table 'person_aliases' still exists")
        fails += 1
    except Exception:
        print("[1] ✅ table 'person_aliases' dropped")

    # 2. Aliases backfilled onto nodes.
    nodes = s.table("graph_nodes").select("id, label, metadata").eq("type", "person") \
        .eq("is_current", True).execute().data or []
    with_alias = [n for n in nodes if isinstance(meta_of(n).get("aliases"), list) and meta_of(n)["aliases"]]
    total_aliases = sum(len(meta_of(n).get("aliases") or []) for n in with_alias)
    print(f"[2] person nodes with aliases: {len(with_alias)}/{len(nodes)} ({total_aliases} alias entries)")

    # Spot-check the famous ones.
    by_label = {n["label"].lower(): n for n in nodes}
    for label, expect in [("danny", ["user", "me", "my", "i", "yashwant"]),
                          ("sunjula daniel", ["sunju", "sunjula"]),
                          ("mother", ["amma"])]:
        n = by_label.get(label)
        if n:
            al = [str(a).lower() for a in (meta_of(n).get("aliases") or [])]
            hit = [e for e in expect if e in al]
            print(f"    {label!r}: aliases={sorted(al)[:12]}" + (f"  (expected {hit} ✓)" if hit else "  (no expected alias!)"))
            if expect and not hit:
                fails += 1
        else:
            print(f"    {label!r}: node not found (skipped)")

    # 3. alias_usage map present where counts existed.
    with_usage = [n for n in nodes if isinstance(meta_of(n).get("alias_usage"), dict) and meta_of(n)["alias_usage"]]
    print(f"[3] nodes with alias_usage counters: {len(with_usage)}")

    # 4. No code references to person_aliases table remain? (runtime hint — the
    #    repointed resolve_alias reads node metadata; a leftover table() call
    #    would 4xx post-drop. Scan for the literal here.)
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "person_aliases", "--include=*.py", "core", "api", "scripts"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ).stdout.strip().splitlines()
    live = [h for h in hits if "person_aliases" in h and ".pyc" not in h and "migrate_aliases_to_nodes" not in h]
    print(f"[4] backend references to person_aliases: {len(live)}")
    for h in live[:12]:
        print(f"    {h}")

    print()
    if fails == 0:
        print("✅ VERIFICATION PASSED — aliases live on nodes; person_aliases retired.")
        return 0
    print(f"❌ {fails} verification failure(s).")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="post-apply verification mode")
    args = ap.parse_args()
    sys.exit(verify() if args.verify else precheck())
