"""Per-run isolation for the live-DB suites (plans/75 §7, ledger X4/X5).

X4 — per-run chat allocation
----------------------------
The sim/cluster/UAT suites insert rows keyed by chat_id (workflows, threads,
messages) with FIXED ids (999999999, 9000000+offset, 909999999). CI + local +
the manual phone all share the one Test tenant, so two concurrent runs race on
the same ids (thread UUID PK collisions, workflow row stomping). Each pytest
process instead draws a unique band of chat ids (9.1M–9.99M, below real
Telegram ids like Danny's 756M) so a run's chat-keyed rows can never collide
with another run's.

X5 — clean-slate pre-delete
---------------------------
Only the post-session leak guard existed; a run killed mid-way leaves its
marker rows behind, which poison the next run (PK collisions on reseed, and
stale rows that mask real leaks). `pre_delete_test_rows()` purges test-marker
rows owned by the test tenant BEFORE the suite runs, making each run
self-healing. Rows owned by ANY OTHER tenant are deliberately NOT deleted —
that is exactly what the fail-closed leak guard must keep catching.
"""

import os
import socket
import time

from tests.fixtures.test_tenant import fresh_supabase
from core.lib.redis_cache import get_redis


# ── Cross-machine sandbox lock (X4 residual) ──────────────────────────────
# Chat allocation (X4) isolates chat/thread-keyed rows, but marker-title
# sweeps ([SIM_TEST]% in tasks.title etc.) still cross truly-concurrent
# runs: every run's teardown deletes every run's marker rows because they
# share the Test tenant + marker text. The fix is a cross-machine lock via
# Upstash Redis (already wired for the rate limiter) — SET NX EX, so the
# second live run fails fast with a clear message instead of racing, and a
# killed run's lock self-expires via TTL (pairs with the X5 clean-slate
# self-healing). Redis unconfigured → get_redis() returns None → no lock
# (hermetic/env-less runs unaffected).
_SANDBOX_LOCK_KEY = "rhodey:test-sandbox:lock"
_SANDBOX_LOCK_TTL_S = int(os.getenv("SANDBOX_LOCK_TTL_S", "2700"))  # 45 min
_SANDBOX_LOCK_WAIT_S = int(os.getenv("SANDBOX_LOCK_WAIT_S", "60"))  # bounded wait


class SandboxLockHeldError(RuntimeError):
    """Another live-DB run holds the sandbox lock."""


def _lock_token() -> str:
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown-host"
    return f"{host}:pid{os.getpid()}:{int(time.time())}"


def acquire_sandbox_lock(client=None) -> dict | None:
    """Acquire the cross-machine sandbox lock (X4 residual).

    Returns the lock handle (token + client) on success; raises
    SandboxLockHeldError when another live run still holds it after the
    bounded wait (fail-closed); returns None when Redis is unavailable (no
    cross-machine serialization possible — callers proceed as before).
    """
    if client is None:
        client = get_redis()
    if client is None:
        return None
    token = _lock_token()
    deadline = time.time() + _SANDBOX_LOCK_WAIT_S
    while True:
        acquired = client.set(_SANDBOX_LOCK_KEY, token,
                              ex=_SANDBOX_LOCK_TTL_S, nx=True)
        if acquired:
            return {"token": token, "client": client}
        if time.time() >= deadline:
            holder = None
            try:
                holder = client.get(_SANDBOX_LOCK_KEY)
            except Exception:
                pass
            raise SandboxLockHeldError(
                f"Another live-DB run holds the sandbox lock "
                f"(holder={holder!r}, ttl={_SANDBOX_LOCK_TTL_S}s). "
                "Wait for it to finish, or raise SANDBOX_LOCK_WAIT_S / "
                "clear the stale lock manually."
            )
        time.sleep(min(15, max(1.0, deadline - time.time())))


def release_sandbox_lock(lock) -> None:
    """Release the lock — only if we still own it, never clearing another
    run's lock. Fail-open: the TTL self-clears, so a release hiccup must
    never fail the suite."""
    if not lock:
        return
    try:
        client = lock["client"]
        if client.get(_SANDBOX_LOCK_KEY) == lock["token"]:
            client.delete(_SANDBOX_LOCK_KEY)
    except Exception:
        pass

# Per-process chat band. Must be:
#   - stable within the process (module-level constant)
#   - different across concurrent processes (pid + wall clock)
#   - outside the legacy fixed band (9,000,000–9,099,999) and below real
#     Telegram chat ids (8–9 digits, e.g. Danny's 756,478,183)
RUN_CHAT_BASE = 9100000 + (os.getpid() * 1000 + int(time.time())) % 890000

# Contiguous offsets the suites allocate within the run band:
#   sim seed          → run_chat_id()        (+0)
#   suite2            → run_chat_id(1)       (+1)
#   UAT               → run_chat_id(2)       (+2)
#   note_capture      → run_chat_id() + n    (+0..+19)
# The leak guard covers range(RUN_CHAT_BASE, RUN_CHAT_BASE + 32).
RUN_CHAT_SPAN = 32


def run_chat_id(offset: int = 0) -> int:
    """Run-unique chat id at `offset` within this process's band."""
    return RUN_CHAT_BASE + offset


def run_thread_uuid(seed: int = 0) -> str:
    """Run-unique conversation_threads id, keeping the fixed
    '00000000-0000-4000-8000' prefix the leak guard keys on.

    The tail is derived from the run band + seed, so concurrent runs never
    hit the same PK (the old fixed '...aaaa' tails collided across runs).
    """
    tail = f"{(RUN_CHAT_BASE + seed) % 0xFFFFFFFFFFFF:012x}"
    return f"00000000-0000-4000-8000-{tail}"


# (table, marker column) pairs whose rows carry suite markers. Mirrors the
# leak guard's _LEAK_MARKER_TABLES; children appear before parents so the
# deletes never violate FK ordering (NO ACTION edges live outside this set).
# conversation_threads / conversation_workflows are NOT here — they are
# chat/thread-keyed and handled by the pre-delete block above.
_MARKER_ROWS = [
    ("decisions", "title"),
    ("tasks", "title"),
    ("memories", "content"),
    ("raw_dumps", "content"),
    ("resources", "url"),
    ("audit_logs", "message"),
    ("projects", "name"),
    ("graph_nodes", "label"),
    ("organizations", "name"),  # dropped by migration 75 — no-op when missing
]

_THREAD_PREFIX = "00000000-0000-4000-8000"


def _test_chat_ids() -> list[int]:
    """Chat ids this run writes + the legacy fixed ids still on the sandbox."""
    return sorted(
        set(range(RUN_CHAT_BASE, RUN_CHAT_BASE + RUN_CHAT_SPAN))
        | {999999999, 909999999}
    )


def pre_delete_test_rows(uid: str) -> None:
    """Clean-slate purge of test-tenant marker rows (X5).

    Deletes marker rows owned by the test tenant + chat/thread-keyed test rows
    BEFORE the suite, so a killed run's residue cannot poison this one. Rows
    owned by any other tenant are left untouched — the post-session leak
    guard is the enforcement point for real cross-tenant leaks.
    """
    supabase = fresh_supabase()
    chat_ids = _test_chat_ids()

    # Chat-keyed rows first (workflows reference threads; both keyed by
    # test chat ids / thread prefix rather than [TEST]-style titles).
    try:
        supabase.table("conversation_workflows") \
            .delete() \
            .in_("chat_id", chat_ids) \
            .eq("owner_id", uid) \
            .execute()
    except Exception:
        pass  # table missing / column mismatch → nothing to purge
    try:
        supabase.table("conversation_threads") \
            .delete() \
            .ilike("id", f"{_THREAD_PREFIX}%") \
            .eq("owner_id", uid) \
            .execute()
    except Exception:
        pass

    # Marker rows, children before parents, owner-scoped.
    for table, col in _MARKER_ROWS:
        try:
            supabase.table(table) \
                .delete() \
                .ilike(col, "[TEST]%") \
                .eq("owner_id", uid) \
                .execute()
            supabase.table(table) \
                .delete() \
                .ilike(col, "[SIM_TEST]%") \
                .eq("owner_id", uid) \
                .execute()
            supabase.table(table) \
                .delete() \
                .ilike(col, "[UAT]%") \
                .eq("owner_id", uid) \
                .execute()
        except Exception:
            continue  # table missing / column mismatch → not a leak signal
