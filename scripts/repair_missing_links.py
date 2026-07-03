"""Repair missing passage-phrase links for enriched passages.

525/855 passages (61%) have zero phrase node links because build_triple_graph
failed to upsert them (342 link upsert failures — duplicate constraint collisions).

This script:
1. Finds passages with enrichment prefix [retrieval, ...]
2. Excludes passages that already have links
3. Parses entity labels from the prefix
4. Resolves node_ids from retrieval_phrase_nodes
5. Batch upserts links with role="mention"

Idempotent: safe to re-run. Uses dedup (same passage+node+role) so Fix A's
pattern is replicated here to prevent any Postgres conflict errors.
"""
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREFIX_RE = re.compile(r'^\[retrieval,\s*(.+?)\]')


def parse_entity_labels(text: str) -> list[str]:
    m = PREFIX_RE.match(text)
    if not m:
        return []
    parts = [p.strip() for p in m.group(1).split(",")]
    seen = set()
    labels = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            labels.append(p)
    return labels


def main():
    from core.services.db import get_supabase
    from core.lib.audit_logger import audit_log_sync

    supabase = get_supabase()

    enriched = supabase.table("retrieval_passages") \
        .select("id, text") \
        .like("text", "[retrieval,%") \
        .execute()
    if not enriched.data:
        print("No enriched passages found.")
        return

    enriched_map = {p["id"]: p["text"] for p in enriched.data}
    print(f"Found {len(enriched_map)} enriched passages.")

    linked = supabase.table("retrieval_passage_phrase_links") \
        .select("passage_id") \
        .in_("passage_id", list(enriched_map.keys())) \
        .execute()
    linked_ids = set(row["passage_id"] for row in (linked.data or []))
    print(f"{len(linked_ids)} enriched passages already have links.")

    links_to_create = []
    total_entities = 0
    unparseable = 0

    for pid, text in enriched_map.items():
        if pid in linked_ids:
            continue
        labels = parse_entity_labels(text)
        if not labels:
            unparseable += 1
            continue
        for label in labels:
            links_to_create.append({"passage_id": pid, "normalized_text": label})
            total_entities += 1

    to_fix = len(enriched_map) - len(linked_ids)
    print(f"{to_fix} enriched passages need links.")
    print(f"{total_entities} entity labels to resolve ({unparseable} passages had unparseable prefix).")

    if not links_to_create:
        print("All enriched passages already linked. Nothing to do.")
        return

    all_labels = list(set(row["normalized_text"] for row in links_to_create))
    node_map = {}
    batch_size = 50
    for i in range(0, len(all_labels), batch_size):
        batch = all_labels[i:i + batch_size]
        rows = supabase.table("retrieval_phrase_nodes") \
            .select("id, normalized_text") \
            .in_("normalized_text", batch) \
            .execute()
        for r in (rows.data or []):
            node_map[r["normalized_text"]] = r["id"]

    print(f"Resolved {len(node_map)}/{len(all_labels)} entity labels to node_ids.")

    unresolved = set(all_labels) - set(node_map.keys())
    if unresolved:
        print(f"WARNING: {len(unresolved)} labels not found in retrieval_phrase_nodes:")
        for u in sorted(unresolved):
            print(f"  - {u}")

    new_links = []
    for entry in links_to_create:
        nid = node_map.get(entry["normalized_text"])
        if nid:
            new_links.append({
                "passage_id": entry["passage_id"],
                "node_id": nid,
                "role": "mention",
                "weight": 1.0,
            })

    if not new_links:
        print("No links to create.")
        return

    seen = set()
    unique_links = []
    for link in new_links:
        key = (link["passage_id"], link["node_id"], link["role"])
        if key not in seen:
            seen.add(key)
            unique_links.append(link)

    print(f"Creating {len(unique_links)} links (deduped from {len(new_links)})...")

    link_batch_size = 500
    for i in range(0, len(unique_links), link_batch_size):
        batch = unique_links[i:i + link_batch_size]
        supabase.table("retrieval_passage_phrase_links") \
            .upsert(batch, on_conflict="passage_id,node_id,role") \
            .execute()
        print(f"  Batch {i // link_batch_size + 1}: {len(batch)} links upserted.")

    linked_after = supabase.table("retrieval_passage_phrase_links") \
        .select("passage_id") \
        .in_("passage_id", list(enriched_map.keys())) \
        .execute()
    linked_after_ids = set(row["passage_id"] for row in (linked_after.data or []))

    gained = len(linked_after_ids) - len(linked_ids)
    print(f"\nDone. {len(enriched_map)} enriched passages total.")
    print(f"  Before: {len(linked_ids)} with links")
    print(f"  After:  {len(linked_after_ids)} with links")
    print(f"  Gain:   {gained} newly linked passages")

    audit_log_sync("retrieval", "INFO",
                   f"repair_missing_links: created {len(unique_links)} links for "
                   f"{gained} previously unlinked passages")


if __name__ == "__main__":
    main()
