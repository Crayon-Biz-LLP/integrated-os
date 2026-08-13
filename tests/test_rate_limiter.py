import time
import pytest

from core.lib.rate_limiter import SlidingWindowLimiter, sentinel_flash_limiter, flash_3_5_limiter
from core.lib import redis_cache
from core.lib.redis_cache import redis_rate_limit_check


def test_sliding_window_fallback():
    limiter = SlidingWindowLimiter(max_calls=5, per_seconds=60)
    
    # Should allow 5 calls with wait=0
    for _ in range(5):
        assert limiter._get_wait_secs() == 0.0
        limiter.acquire()
        
    # 6th call should require wait
    wait_time = limiter._get_wait_secs()
    assert wait_time > 0.0


# ──────────────────────────────────────────
# Self-feeding starvation regression (the pulse outage)
# ──────────────────────────────────────────

class _FakeSortedSetPipeline:
    """Minimal in-memory sorted set replaying the exact ops redis_rate_limit_check issues."""
    def __init__(self, store):
        self.store = store
        self.ops = []

    def zremrangebyscore(self, key, min_, max_):
        self.ops.append(("zremrangebyscore", key, min_, max_))
        return self

    def zcard(self, key):
        self.ops.append(("zcard", key))
        return self

    def zrange(self, key, start, stop, withscores=False):
        self.ops.append(("zrange", key, start, stop, withscores))
        return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def exec(self):
        results = []
        for op in self.ops:
            kind = op[0]
            if kind == "zremrangebyscore":
                _, key, min_, max_ = op
                ss = self.store.setdefault(key, {})
                drop = [m for m, s in ss.items() if min_ <= s <= max_]
                for m in drop:
                    del ss[m]
                results.append(len(drop))
            elif kind == "zcard":
                results.append(len(self.store.setdefault(op[1], {})))
            elif kind == "zrange":
                _, key, start, stop, withscores = op
                ss = self.store.setdefault(key, {})
                ordered = sorted(ss.items(), key=lambda kv: kv[1])
                sl = ordered[start:stop + 1]
                results.append([(m, s) for m, s in sl] if withscores else [m for m, _ in sl])
            elif kind == "zadd":
                _, key, mapping = op
                self.store.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif kind == "expire":
                results.append(True)
        self.ops = []
        return results


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakeSortedSetPipeline(self.store)

    def zcard(self, key):
        return len(self.store.get(key, {}))


def test_blocked_polls_do_not_grow_the_set(monkeypatch):
    """Regression for the pulse outage: a blocked caller's poll must never add
    a member. Pre-fix (zadd-before-check) a max=4 limiter grew to 12 under 8
    blocked polls — reproducing that here must now stay pinned at 4."""
    fake = _FakeRedis()
    monkeypatch.setattr(redis_cache, "get_redis", lambda: fake)
    key = "rhodey:rate_limit:test:regression"

    # 4 allowed callers fill the window exactly.
    for _ in range(4):
        allowed, wait = redis_rate_limit_check(key, 4, 60)
        assert allowed is True and wait == 0.0
    assert fake.zcard(key) == 4

    # 8 blocked polls — wait is reported, but the set must NOT grow.
    for _ in range(8):
        allowed, wait = redis_rate_limit_check(key, 4, 60)
        assert allowed is False
        assert wait > 0.0
    assert fake.zcard(key) == 4  # the regression assertion

    # Once the window slides (members age out), a poll is admitted again.
    # Simulate expiry by clearing the set, then verify re-admission.
    fake.store[key].clear()
    allowed, wait = redis_rate_limit_check(key, 4, 60)
    assert allowed is True and wait == 0.0
    assert fake.zcard(key) == 1


def test_under_limit_admission_adds_one_member(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_cache, "get_redis", lambda: fake)
    key = "rhodey:rate_limit:test:admission"

    for i in range(3):
        allowed, wait = redis_rate_limit_check(key, 4, 60)
        assert allowed is True
        assert fake.zcard(key) == i + 1


# ──────────────────────────────────────────
# Bounded async acquire (no worker can hang)
# ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_async_bails_after_wait_budget(monkeypatch):
    limiter = SlidingWindowLimiter(max_calls=1, per_seconds=60)
    # Simulate a saturated limiter that never frees a token.
    monkeypatch.setattr(limiter, "_get_wait_secs", lambda: 10.0)

    start = time.time()
    await limiter.acquire_async(max_total_wait=1.0)  # budget 1s
    elapsed = time.time() - start

    # Returns promptly instead of recursing forever (pre-fix hung 900s).
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_acquire_async_admits_when_token_available(monkeypatch):
    limiter = SlidingWindowLimiter(max_calls=1, per_seconds=60)
    waits = iter([5.0, 0.0])  # blocked once, then admitted
    monkeypatch.setattr(limiter, "_get_wait_secs", lambda: next(waits))

    await limiter.acquire_async(max_total_wait=30.0)
    # No exception and returned — the loop admits after the window slides.


# ──────────────────────────────────────────
# Sentinel limiter isolation
# ──────────────────────────────────────────

def test_sentinel_limiter_uses_its_own_pool():
    assert sentinel_flash_limiter.prefix != flash_3_5_limiter.prefix
    assert sentinel_flash_limiter.prefix == "sentinel_flash"


# ──────────────────────────────────────────
# Limiter override threading (sentinel split)
# ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_threads_limiter_override_to_gemini(monkeypatch):
    from core.llm import fallback as fallback_mod
    from core.llm.config import WorkloadProfile

    seen = {}
    async def fake_gemini(**kwargs):
        seen["limiter"] = kwargs.get("limiter")
        return ("ok", None, None)

    monkeypatch.setattr(fallback_mod, "call_gemini", fake_gemini)

    override = object()
    resp = await fallback_mod.generate_content_with_fallback(
        prompt="hi",
        workload=WorkloadProfile.INTERACTIVE,
        limiter=override,
    )
    assert seen.get("limiter") is override
    assert resp.success is True


@pytest.mark.asyncio
async def test_fallback_without_override_passes_no_limiter(monkeypatch):
    from core.llm import fallback as fallback_mod
    from core.llm.config import WorkloadProfile

    seen = {}
    async def fake_gemini(**kwargs):
        seen["limiter"] = kwargs.get("limiter")
        return ("ok", None, None)

    monkeypatch.setattr(fallback_mod, "call_gemini", fake_gemini)

    await fallback_mod.generate_content_with_fallback(
        prompt="hi",
        workload=WorkloadProfile.INTERACTIVE,
    )
    assert seen.get("limiter") is None
