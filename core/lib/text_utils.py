"""Shared text utilities — extracted from backfill_graph.py (Sep 2026).

Functions here are used by concept_sweep_batch.py and potentially other
modules. Moved out of backfill_graph.py when it was retired.
"""

import json


def normalize_meta(raw) -> dict:
    """Normalize graph node metadata to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, list):
        return {}
    return {}


def synthesize_content(memory: dict) -> str:
    """Build a text string from a memory dict for embedding/search indexing.

    Used by concept_sweep_batch.py and backfill embedding paths.
    """
    memory_type = memory.get("memory_type", "")
    content = memory.get("content", "")
    metadata = normalize_meta(memory.get("metadata"))

    if memory_type == "Prophecy":
        entry_type = metadata.get("entry_type", "")
        return f"[PROPHECY:{entry_type}] {content}" if entry_type else content

    elif memory_type in ["Psalm", "Prayer"]:
        tags = metadata.get("tags", "")
        if tags:
            return f"[TAGS:{tags}] {content}"
        return content

    return content
