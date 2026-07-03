"""Seed retrieval_eval_gold with ground-truth labels.

Usage:
    python -m core.retrieval.seed_eval_gold [--dry-run]

Reads SEED_DATA below and upserts into retrieval_eval_gold.
Each entry maps a query to expected memory IDs (resolved by content fragment).

To add new entries, append to SEED_DATA. Each entry needs:
- query_text: the evaluation query
- content_fragments: list of substrings that appear in expected memories
- category: entity_lookup | topic_query | temporal | ambiguous | general
- notes: optional explanation of why these are the expected results
"""

import asyncio
import json
from typing import List, Dict
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()

# ---------------------------------------------------------------------------
# Seed data — 18 queries covering all categories.
# content_fragments are substrings used to find matching memory IDs.
# After seeding, manually verify and replace content_fragments with actual
# memory IDs in the DB if needed.
# ---------------------------------------------------------------------------
SEED_DATA: List[Dict] = [
    # --- Entity lookup (7) ---
    {
        "query_text": "Who is Binu?",
        "content_fragments": ["Binu"],
        "category": "entity_lookup",
        "notes": "Should return memories mentioning Binu by name",
    },
    {
        "query_text": "What should I remember before meeting Ashraya?",
        "content_fragments": ["Ashraya", "church"],
        "category": "entity_lookup",
        "notes": "Should surface Ashraya-related and church-related memories",
    },
    {
        "query_text": "Which people are connected to QHORD?",
        "content_fragments": ["QHORD"],
        "category": "entity_lookup",
        "notes": "Should return memories about QHORD project and collaborators",
    },
    {
        "query_text": "Who have I discussed the Crayon project with?",
        "content_fragments": ["Crayon"],
        "category": "entity_lookup",
        "notes": "Should surface Crayon-related memories with people context",
    },
    {
        "query_text": "What do I know about Armour Cyber?",
        "content_fragments": ["Armour Cyber", "Armour"],
        "category": "entity_lookup",
        "notes": "Should find multi-word entity label",
    },
    {
        "query_text": "Tell me about Shifrah",
        "content_fragments": ["Shifrah"],
        "category": "entity_lookup",
        "notes": "Person entity lookup",
    },
    {
        "query_text": "What is SOLVSTRAT?",
        "content_fragments": ["SOLVSTRAT", "Solvstrat"],
        "category": "entity_lookup",
        "notes": "Should surface SOLVSTRAT project memories",
    },
    # --- Topic query (5) ---
    {
        "query_text": "What earlier decision is relevant for Solvstrat?",
        "content_fragments": ["Solvstrat", "SOLVSTRAT", "decision"],
        "category": "topic_query",
        "notes": "Should surface decisions related to SOLVSTRAT",
    },
    {
        "query_text": "What do I know about the church operations issue?",
        "content_fragments": ["church", "operations", "ASHRAYA"],
        "category": "topic_query",
        "notes": "Should surface church/operations memories",
    },
    {
        "query_text": "What is the QHORD pricing strategy?",
        "content_fragments": ["QHORD", "pricing"],
        "category": "topic_query",
        "notes": "Topic query combining project + concept",
    },
    {
        "query_text": "What legal matters are pending for Crayon?",
        "content_fragments": ["Crayon", "legal", "tax"],
        "category": "topic_query",
        "notes": "Should surface CRAYON-tagged legal/governance memories",
    },
    {
        "query_text": "What email threads are important this week?",
        "content_fragments": ["email", "thread", "important"],
        "category": "topic_query",
        "notes": "General topic query about email activity",
    },
    # --- Temporal (3) ---
    {
        "query_text": "What deadlines are approaching this week?",
        "content_fragments": ["deadline", "due", "week"],
        "category": "temporal",
        "notes": "Should surface time-sensitive memories",
    },
    {
        "query_text": "What meetings did I have yesterday?",
        "content_fragments": ["meeting", "yesterday"],
        "category": "temporal",
        "notes": "Temporal query for recent meetings",
    },
    {
        "query_text": "What happened this morning?",
        "content_fragments": ["morning", "today"],
        "category": "temporal",
        "notes": "Short temporal query — may be ambiguous",
    },
    # --- Ambiguous (3) ---
    {
        "query_text": "What pattern is repeating in my week?",
        "content_fragments": ["pattern", "recurring", "week"],
        "category": "ambiguous",
        "notes": "Abstract query — tests graph traversal for thematic connections",
    },
    {
        "query_text": "What should I be worried about?",
        "content_fragments": ["risk", "blocker", "deadline", "worry"],
        "category": "ambiguous",
        "notes": "Highly ambiguous — tests ability to surface flagged items",
    },
    {
        "query_text": "Tell me something I might have forgotten",
        "content_fragments": [],
        "category": "ambiguous",
        "notes": "Open-ended — tests recency and importance weighting",
    },
]


async def resolve_fragments_to_ids(
    fragments: List[str], top_k: int = 5
) -> List[int]:
    """Resolve content fragments to memory IDs via associative retrieval.

    For each fragment, runs a quick retrieval and collects memory IDs.
    Returns deduplicated list.
    """
    from core.retrieval.search import associative_retrieve

    all_ids = []
    for fragment in fragments:
        try:
            bundle = await associative_retrieve(query=fragment, top_k=top_k)
            for item in bundle.items:
                if item.memory_id not in all_ids:
                    all_ids.append(item.memory_id)
        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"resolve_fragments: failed for '{fragment}': {e}")
    return all_ids


async def seed(dry_run: bool = False) -> dict:
    """Seed ground-truth data.

    If content_fragments are provided, resolves them to memory IDs first.
    If expected_memory_ids are provided directly, uses those.
    """
    from core.retrieval.eval import seed_ground_truth

    entries = []
    for item in SEED_DATA:
        expected_ids = item.get("expected_memory_ids", [])

        # Resolve fragments to IDs if no explicit IDs provided
        if not expected_ids and item.get("content_fragments"):
            if not dry_run:
                expected_ids = await resolve_fragments_to_ids(
                    item["content_fragments"]
                )
            else:
                expected_ids = []  # dry run — skip resolution

        entries.append({
            "query_text": item["query_text"],
            "expected_memory_ids": expected_ids,
            "category": item.get("category", "general"),
            "notes": item.get("notes"),
        })

    if dry_run:
        print(f"[SEED] Dry run: would insert {len(entries)} entries")
        for e in entries:
            ids_display = e["expected_memory_ids"] or "(fragments not resolved)"
            print(f"  [{e['category']}] {e['query_text'][:50]} → {ids_display}")
        return {"status": "dry_run", "entries": len(entries)}

    count = seed_ground_truth(entries)
    print(f"[SEED] Seeded {count}/{len(entries)} ground-truth entries")
    return {"status": "completed", "seeded": count, "total": len(entries)}


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv
    result = asyncio.run(seed(dry_run=dry_run))
    print(json.dumps(result, indent=2))
