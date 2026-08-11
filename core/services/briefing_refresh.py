"""Shared live-briefing refresh trigger (Aug 11 fix).

The app's home-screen briefing is supposed to refresh after every meaningful
user action — a task marked done, a task added/cancelled, a snooze, a decision
approved/rejected — so the user never sees a stale briefing that still names a
task they just closed.

Before this module, the rebuild+push lived ONLY inside
`api.index._run_web_message_pipeline` (the send-message path) and swallowed
failures with a bare `print()`. That produced the Gas-Booking ghost: a task
completed from the app went through `_complete_task`, which invalidated the
briefing cache but never rebuilt/pushed — and on the one path that did rebuild,
a network blip failed silently with no retry and no audit trail.

This module is the single source of truth for that refresh:

  - `trigger_briefing_refresh()` — await it when latency is tolerable.
  - `fire_briefing_refresh()`    — background fire-and-forget for API paths
    (task done, snooze, decisions) so the response stays snappy.

Both are audited end-to-end (audit_log_sync), retried once on failure, and
debounced to the briefing cache TTL (120s) so a burst of actions (e.g.
approving 5 inbox items in a row) costs one rebuild plus cheap re-fetch
nudges. Best-effort by contract: `fire_briefing_refresh` invalidates the
cache synchronously BEFORE spawning, so even if the container dies mid-refresh
the app's NEXT fetch rebuilds fresh.
"""

import asyncio
import json
import time

from core.lib.audit_logger import audit_log_sync
from core.services.db import get_tenant, tenant_aware_client

_HOME_FEED_BRIEFING_CACHE_KEY = "rhodey:briefing:home_feed:v1"
_REFRESH_DEBOUNCE_SECONDS = 120  # matches the home-feed briefing cache TTL


def briefing_cache_key() -> str:
    """Tenant-scoped briefing cache key (M5).

    The briefing payload is tenant-specific — a global key would serve
    tenant B tenant A's cached briefing (silent cross-tenant leak). Key on
    the tenant id; unscoped legacy runs keep the bare key.

    Shared public API: api.index uses the same key for its read-through
    home-feed cache so invalidation/repopulation here can never drift.
    """
    uid = get_tenant()
    return f"{_HOME_FEED_BRIEFING_CACHE_KEY}:{uid}" if uid else _HOME_FEED_BRIEFING_CACHE_KEY


# Per-tenant monotonic timestamp of the last REBUILD attempt (debounce gate).
_last_rebuild: dict[str, float] = {}

# Strong references to in-flight background tasks — asyncio keeps only weak
# refs in its internals, so without this a task could be GC'd mid-flight.
_briefing_refresh_tasks: set[asyncio.Task] = set()


def _cache_invalidate(cache_key: str) -> None:
    try:
        from core.lib.redis_cache import cache_delete
        cache_delete(cache_key)
    except Exception as e:
        audit_log_sync("briefing_refresh", "WARNING", f"cache invalidation failed: {e}")


async def trigger_briefing_refresh(source: str = "action") -> dict:
    """Rebuild the live briefing, invalidate the cache, and push a silent
    `briefing_refresh` to the app.

    Runs the exact rebuild + push that send-message used to run, but:
      - invalidates the cache FIRST (a concurrent home-feed fetch must not
        serve the pre-action briefing while we rebuild),
      - repopulates the cache so the app's next fetch is fast,
      - audits every step and retries once on failure (was: silent print),
      - debounces to the cache TTL so rapid actions don't hammer the LLM.

    Returns {"success": bool, ...}. Never raises.
    """
    from api.briefing import build_briefing
    from core.lib.redis_cache import cache_set
    from core.services.push_notification import send_silent_push

    cache_key = briefing_cache_key()
    uid = get_tenant() or "legacy"

    # ── Debounce: a rebuild happened within the cache TTL → the cached
    # payload is already fresh enough; just nudge the app to re-fetch.
    now = time.monotonic()
    if now - _last_rebuild.get(uid, 0.0) < _REFRESH_DEBOUNCE_SECONDS:
        try:
            await send_silent_push({"type": "briefing_refresh"})
            audit_log_sync(
                "briefing_refresh", "INFO",
                f"[{source}] debounced (rebuilt {(now - _last_rebuild.get(uid, 0.0)):.0f}s ago) — nudge sent",
            )
        except Exception as e:
            audit_log_sync("briefing_refresh", "WARNING", f"[{source}] debounced nudge failed: {e}")
        return {"success": True, "debounced": True}

    # The window is marked BEFORE the attempt on purpose: it also debounces
    # concurrent fires of the same action. Trade-off (honest): a persistent
    # failure still suppresses rebuilds for the TTL window (nudge-only), but
    # it is audited as ERROR and the cache invalidation below makes the next
    # fetch rebuild fresh — visible, self-healing, never silent.
    _last_rebuild[uid] = now

    # ── Invalidate FIRST so the stale pre-action briefing is never served.
    _cache_invalidate(cache_key)

    async def _attempt() -> dict:
        supabase = tenant_aware_client()
        briefing = await build_briefing(supabase)
        briefing_update = json.loads(json.dumps(briefing, default=str))

        push_payload: dict = {"type": "briefing_refresh"}
        headline = (
            briefing_update.get("voice_line")
            or briefing_update.get("context_bar")
            or briefing_update.get("greeting", "")
        )
        mode = briefing_update.get("home_mode", "proceed")
        insights_list = []
        if mode == "sprint":
            nxt = briefing_update.get("next_event")
            if nxt:
                insights_list.append({"text": f"🎯 Sprinting: {nxt}", "link": "rhodey://today"})
            v_urg = briefing_update.get("vaulted_urgent_count", 0)
            if v_urg > 0:
                insights_list.append({"text": f"🔴 {v_urg} urgent", "link": "rhodey://surface"})
        elif mode == "decide":
            pend = briefing_update.get("pending_count", 0)
            if pend > 0:
                insights_list.append({"text": f"⚖️ {pend} pending decisions", "link": "rhodey://inbox"})
        else:
            for ins in briefing_update.get("insights", []):
                insights_list.append({"text": ins, "link": "rhodey://surface"})

        push_payload["headline"] = headline
        push_payload["insights_json"] = json.dumps(insights_list)

        await send_silent_push(push_payload)

        # Repopulate the cache so the app's next home-feed fetch is fast
        # instead of blocking on a 9-16s rebuild.
        try:
            cache_set(cache_key, briefing_update, ttl=_REFRESH_DEBOUNCE_SECONDS)
        except Exception:
            pass

        return briefing_update

    try:
        result = await _attempt()
    except Exception as e:
        audit_log_sync("briefing_refresh", "WARNING", f"[{source}] first attempt failed, retrying once: {e}")
        try:
            result = await _attempt()
        except Exception as e2:
            audit_log_sync("briefing_refresh", "ERROR", f"[{source}] refresh failed after retry: {e2}")
            return {"success": False, "error": str(e2)}

    audit_log_sync("briefing_refresh", "INFO", f"[{source}] briefing rebuilt + refresh push sent")
    return {"success": True, "briefing": result}


def fire_briefing_refresh(source: str = "action") -> None:
    """Fire the briefing refresh in the background (non-blocking).

    Safe to call from any async handler without slowing the response. When a
    rebuild is due, the cache is invalidated SYNCHRONOUSLY before the task is
    spawned so a stale briefing can never survive a container restart.

    No running event loop (sync context) → logs and skips; the cache
    invalidation above (when due) still makes the next fetch self-heal.
    """
    uid = get_tenant() or "legacy"
    now = time.monotonic()
    if now - _last_rebuild.get(uid, 0.0) >= _REFRESH_DEBOUNCE_SECONDS:
        # A rebuild is due — invalidate synchronously so the self-heal
        # guarantee holds even if the process dies before the task runs.
        _cache_invalidate(briefing_cache_key())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop (sync context) — never create the coroutine
        # in the first place (an orphaned coroutine is never awaited).
        audit_log_sync(
            "briefing_refresh", "WARNING",
            f"[{source}] no running event loop — skipping background refresh",
        )
        return
    task = asyncio.create_task(trigger_briefing_refresh(source=source))
    _briefing_refresh_tasks.add(task)
    task.add_done_callback(_briefing_refresh_tasks.discard)
