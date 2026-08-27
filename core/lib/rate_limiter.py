import time
import asyncio
from threading import Lock
import os
from core.lib.redis_cache import redis_rate_limit_check, get_redis
from core.lib.audit_logger import audit_log_sync

class SlidingWindowLimiter:
    """Sliding window rate limiter. Thread-safe, works with both sync and async."""

    def __init__(self, max_calls: int, per_seconds: int = 60, redis_key: str = None):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.redis_key = redis_key
        self.timestamps = []
        self.lock = Lock()

    def _prune(self, now: float):
        cutoff = now - self.per_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def _wait_secs_local(self, now: float) -> float:
        self._prune(now)
        if len(self.timestamps) >= self.max_calls:
            wait = self.timestamps[0] + self.per_seconds - now
            return max(wait, 0)
        return 0.0

    def _get_wait_secs(self) -> float:
        if self.redis_key:
            res = redis_rate_limit_check(self.redis_key, self.max_calls, self.per_seconds)
            if res is not None:
                allowed, wait = res
                return wait
        
        # Fallback
        now = time.time()
        return self._wait_secs_local(now)

    def acquire(self):
        """Synchronous acquire — blocks until a token is available."""
        with self.lock:
            wait = self._get_wait_secs()
            if wait > 0:
                time.sleep(wait)
                
            # Keep local timestamps updated just in case Redis goes down
            now = time.time()
            self._prune(now)
            self.timestamps.append(now)

    async def acquire_async(self, max_total_wait: float = 120.0):
        """Asynchronous acquire — awaits until a token is available.

        Bounded: if the total time spent waiting exceeds `max_total_wait`
        (a saturated shared limiter, a misbehaving Redis, or a stuck peer),
        stop waiting, audit a warning, and proceed WITHOUT a token. A caller
        must never block indefinitely — the previous unbounded recursion
        turned a temporary rate-limit block into a worker hang until Modal's
        900s timeout killed it (the pulse outage). With the window at 60s,
        120s covers one full slide plus re-check headroom.
        """
        def _sync_acquire():
            with self.lock:
                w = self._get_wait_secs()
                if w == 0:
                    n = time.time()
                    self._prune(n)
                    self.timestamps.append(n)
                return w

        waited = 0.0
        while True:
            wait = await asyncio.to_thread(_sync_acquire)
            if wait <= 0:
                return
            waited += wait
            if waited >= max_total_wait:
                audit_log_sync(
                    "rate_limiter", "WARNING",
                    f"Limiter wait budget exhausted ({waited:.0f}s >= {max_total_wait:.0f}s) "
                    f"for '{self.redis_key or 'local'}' — proceeding without a token",
                )
                return
            await asyncio.sleep(wait)


def _seconds_until_midnight_utc() -> int:
    """Seconds from now until the next midnight UTC. Used as TTL for RPD counters."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((tomorrow - now).total_seconds()) + 60  # +60s safety buffer


class MultiKeyLimiter:
    """
    Intelligently routes requests across multiple keys via round-robin.
    Uses one unified sliding window that scales by the number of loaded keys.
    Optionally enforces per-key daily (RPD) limits via Redis counters.
    """
    def __init__(self, prefix: str, max_rpm_per_key: int, max_rpd_per_key: int = 0):
        self.prefix = prefix
        self.max_rpm_per_key = max_rpm_per_key
        self.max_rpd_per_key = max_rpd_per_key  # 0 = no RPD guard
        self.limiter = None
        self.lock = Lock()
        self.current_idx = 0
        self._num_keys = 0
        
    def _ensure_initialized(self):
        if self.limiter is None:
            keys = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3"), os.getenv("GEMINI_API_KEY_4")]
            valid_keys = [k for k in keys if k]
            self._num_keys = len(valid_keys) if valid_keys else 1
            
            total_rpm = self._num_keys * self.max_rpm_per_key
            self.limiter = SlidingWindowLimiter(
                max_calls=total_rpm, 
                per_seconds=60, 
                redis_key=f"rhodey:rate_limit:multi:{self.prefix}"
            )

    def _rpd_available(self, key_idx: int) -> bool:
        """Check if a key has remaining daily capacity. Does NOT increment —
        call record_usage() after a successful API call to count it."""
        if self.max_rpd_per_key <= 0:
            return True  # No RPD guard configured
        
        client = get_redis()
        if client is None:
            return True  # Fail open if Redis unavailable
        
        rpd_key = f"rhodey:rpd:{self.prefix}:key{key_idx}"
        try:
            current = int(client.get(rpd_key) or 0)
            return current < self.max_rpd_per_key
        except Exception as e:
            audit_log_sync("rate_limiter", "WARNING", f"RPD check failed for {rpd_key}: {e}")
            return True  # Fail open

    def record_usage(self, key_idx: int) -> None:
        """Record a successful API call against a key's daily quota.
        Must be called AFTER a successful response — never before."""
        if self.max_rpd_per_key <= 0:
            return
        client = get_redis()
        if client is None:
            return
        rpd_key = f"rhodey:rpd:{self.prefix}:key{key_idx}"
        try:
            new_val = client.incr(rpd_key)
            if new_val == 1:
                client.expire(rpd_key, _seconds_until_midnight_utc())
        except Exception as e:
            audit_log_sync("rate_limiter", "WARNING", f"RPD record_usage failed for {rpd_key}: {e}")
            
    async def acquire_async(self) -> int:
        """Awaits until capacity is available, then returns the index of the key to use.
        
        Skips keys that have hit their daily (RPD) limit, rotating to the next
        key with headroom. If all keys are exhausted, returns -1 so the caller
        can fall back to an alternate provider.
        """
        self._ensure_initialized()
        
        # 1. Wait for global RPM capacity
        await self.limiter.acquire_async()
        
        # 2. Find next key with RPD headroom (try all keys once)
        with self.lock:
            for _ in range(self._num_keys):
                idx = self.current_idx
                self.current_idx = (self.current_idx + 1) % self._num_keys
                if self._rpd_available(idx):
                    return idx
            
            # All keys at RPD limit — audit and return -1
            audit_log_sync(
                "rate_limiter", "WARNING",
                f"All {self._num_keys} keys exhausted RPD for '{self.prefix}' "
                f"(limit={self.max_rpd_per_key}/key) — falling back"
            )
            return -1

# Global smart limiters
# Gemini 3.5 Flash Lite (Free tier: 15 RPM, 500 RPD per key).
flash_lite_limiter = MultiKeyLimiter(prefix="flash_lite", max_rpm_per_key=13, max_rpd_per_key=498)

# Gemini 3.6 Flash (Free tier: 5 RPM, 20 RPD per key).
flash_3_5_limiter = MultiKeyLimiter(prefix="flash_3_5", max_rpm_per_key=4, max_rpd_per_key=18)

# Sentinel workloads get their own pool so a sentinel burst can never starve
# pulse briefings (and vice-versa) on the shared flash limiter — the two
# workloads are independent and must fail independently.
sentinel_flash_limiter = MultiKeyLimiter(prefix="sentinel_flash", max_rpm_per_key=4)

# Gemini Embedding (Free tier: 1500 RPM). We use 1400 for safety.
embedding_limiter = MultiKeyLimiter(prefix="embedding", max_rpm_per_key=1400)
