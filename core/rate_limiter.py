import time
import asyncio
from threading import Lock


class SlidingWindowLimiter:
    """Sliding window rate limiter. Thread-safe, works with both sync and async."""

    def __init__(self, max_calls: int, per_seconds: int = 60):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.timestamps = []
        self.lock = Lock()

    def _prune(self, now: float):
        cutoff = now - self.per_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def _wait_secs(self, now: float) -> float:
        self._prune(now)
        if len(self.timestamps) >= self.max_calls:
            wait = self.timestamps[0] + self.per_seconds - now
            return max(wait, 0)
        return 0.0

    def acquire(self):
        """Synchronous acquire — blocks until a token is available."""
        with self.lock:
            now = time.time()
            wait = self._wait_secs(now)
            if wait > 0:
                time.sleep(wait)
                now = time.time()
                self._prune(now)
            self.timestamps.append(now)

    async def acquire_async(self):
        """Asynchronous acquire — awaits until a token is available."""
        with self.lock:
            now = time.time()
            wait = self._wait_secs(now)
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.time()
                self._prune(now)
            self.timestamps.append(now)


# Global shared limiter for gemini-3.1-flash-lite-preview (free tier: 15 RPM)
# Using 12 RPM as ceiling to leave headroom for other processes
flash_lite_limiter = SlidingWindowLimiter(max_calls=12, per_seconds=60)
