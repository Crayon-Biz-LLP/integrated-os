import time
import asyncio
from threading import Lock
import os
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
        def _sync_acquire():
            with self.lock:
                w = self._get_wait_secs()
                if w == 0:
                    n = time.time()
                    self._prune(n)
                    self.timestamps.append(n)
                return w
                
        wait = await asyncio.to_thread(_sync_acquire)

        if wait > 0:
            await asyncio.sleep(wait)
            return await self.acquire_async()


class MultiKeyLimiter:
    """
    Intelligently routes requests across multiple keys via round-robin.
    Uses one unified sliding window that scales by the number of loaded keys.
    """
    def __init__(self, prefix: str, max_rpm_per_key: int):
        self.prefix = prefix
        self.max_rpm_per_key = max_rpm_per_key
        self.limiter = None
        self.lock = Lock()
        self.current_idx = 0
        
    def _ensure_initialized(self):
        if self.limiter is None:
            # Count how many Gemini keys are actually present in the environment
            keys = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3")]
            valid_keys = [k for k in keys if k]
            num_keys = len(valid_keys) if valid_keys else 1
            
            total_rpm = num_keys * self.max_rpm_per_key
            self.limiter = SlidingWindowLimiter(
                max_calls=total_rpm, 
                per_seconds=60, 
                redis_key=f"rhodey:rate_limit:multi:{self.prefix}"
            )
            
    async def acquire_async(self) -> int:
        """Awaits until capacity is available, then returns the index of the key to use."""
        self._ensure_initialized()
        
        # 1. Wait for global capacity
        await self.limiter.acquire_async()
        
        # 2. Get next key in round-robin sequence
        with self.lock:
            idx = self.current_idx
            # Assume 3 keys max for our environment, though we scale RPM dynamically
            keys = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3")]
            valid_keys = [k for k in keys if k]
            num_keys = len(valid_keys) if valid_keys else 1
            
            self.current_idx = (self.current_idx + 1) % num_keys
            return idx

# Global smart limiters
# Gemini 3.1 Flash Lite (Free tier: 15 RPM). We use 13 for safety.
flash_lite_limiter = MultiKeyLimiter(prefix="flash_lite", max_rpm_per_key=13)

# Gemini 3.5 Flash (Free tier: 5 RPM). We use 4 for safety.
flash_3_5_limiter = MultiKeyLimiter(prefix="flash_3_5", max_rpm_per_key=4)

# Gemini Embedding (Free tier: 1500 RPM). We use 1400 for safety.
embedding_limiter = MultiKeyLimiter(prefix="embedding", max_rpm_per_key=1400)
