"""Redis cache for graph edges per node.

PostgREST defaults to 1000 rows per response. graph_edges grows continuously
(Aug 2026: 1,289 rows on Danny's tenant). Unpaginated queries silently
truncate, causing the OS to "forget" connections. This cache stores
edges per node in Redis so lookups are O(1) instead of paginated queries.

Cache key: rhodey:edges:{owner_id}:{node_id}
TTL: 5 minutes (auto-refreshes even if invalidation is missed)

Write path: every graph_edges insert/upsert/delete calls invalidate_node_edges().
Read path:  get_edges_for_node() checks Redis first, falls back to paginated query.
"""
import json
from typing import Optional

from core.lib.audit_logger import audit_log_sync

_CACHE_PREFIX = "rhodey:edges"
_TTL_SECONDS = 300  # 5 minutes


def _cache_key(owner_id: str, node_id: str) -> str:
    return f"{_CACHE_PREFIX}:{owner_id}:{node_id}"


def get_edges_for_node(owner_id: str, node_id: str) -> Optional[list[dict]]:
    """Fetch all edges (source or target) for a node. Cached in Redis.

    Returns list of dicts with keys: source_node_id, target_node_id, relationship.
    Returns None if cache miss (caller should do paginated query and populate).
    """
    try:
        from core.lib.redis_cache import get_redis
        r = get_redis()
        if r is None:
            return None

        key = _cache_key(owner_id, node_id)
        raw = r.get(key)
        if raw is None:
            return None  # cache miss

        return json.loads(raw)
    except Exception as e:
        audit_log_sync("edge_cache", "WARNING", f"get_edges_for_node failed: {e}")
        return None


def set_edges_for_node(owner_id: str, node_id: str, edges: list[dict]) -> None:
    """Populate the cache after a paginated query."""
    try:
        from core.lib.redis_cache import get_redis
        r = get_redis()
        if r is None:
            return

        key = _cache_key(owner_id, node_id)
        r.set(key, json.dumps(edges, default=str), ex=_TTL_SECONDS)
    except Exception as e:
        audit_log_sync("edge_cache", "WARNING", f"set_edges_for_node failed: {e}")


def invalidate_node_edges(owner_id: str, node_ids: list[str]) -> None:
    """Invalidate cached edges for one or more nodes.

    Call this after any graph_edges insert, upsert, or delete.
    """
    try:
        from core.lib.redis_cache import get_redis
        r = get_redis()
        if r is None:
            return

        for nid in node_ids:
            if nid:
                r.delete(_cache_key(owner_id, nid))
    except Exception as e:
        audit_log_sync("edge_cache", "WARNING", f"invalidate_node_edges failed: {e}")
