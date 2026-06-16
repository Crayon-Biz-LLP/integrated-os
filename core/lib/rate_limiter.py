import time
import asyncio
from threading import Lock
from core.lib.redis_cache import redis_rate_limit_check

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

    async def acquire_async(self):
        """Asynchronous acquire — awaits until a token is available."""
        with self.lock:
            wait = self._get_wait_secs()
            if wait > 0:
                await asyncio.sleep(wait)
                
            now = time.time()
            self._prune(now)
            self.timestamps.append(now)


# Global shared limiter for gemini-3.1-flash-lite (free tier: 15 RPM)
# Using 14 RPM as ceiling to safely maximize throughput
flash_lite_limiter = SlidingWindowLimiter(max_calls=13, per_seconds=60, redis_key="rhodey:rate_limit:flash_lite")
