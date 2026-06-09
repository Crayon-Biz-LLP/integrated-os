import os
import json
import time

try:
    from core.lib.audit_logger import audit_log_sync
except Exception:
    def audit_log_sync(service, level, message, metadata=None):
        print(f"[{service}] {level}: {message}")

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

_redis_client = None
_redis_initialized = False

def get_redis():
    global _redis_client, _redis_initialized
    if not _redis_initialized:
        _redis_initialized = True
        if Redis is not None:
            url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("UPSTASH_REDIS_URL")
            token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("UPSTASH_REDIS_TOKEN")
            if url and token:
                try:
                    # Initialize synchronously since we use it in async contexts via thread pool or directly
                    _redis_client = Redis(url=url, token=token)
                except Exception as e:
                    audit_log_sync("redis", "WARNING", f"Failed to initialize Upstash Redis: {e}")
    return _redis_client

def cache_get(key: str):
    """Fetch from Redis. Returns None on miss or error."""
    client = get_redis()
    if client is None:
        return None
    try:
        data = client.get(key)
        if data is None:
            return None
        if isinstance(data, str):
            return json.loads(data)
        return data # upstash_redis might auto-deserialize if it detected json
    except Exception as e:
        audit_log_sync("redis", "WARNING", f"cache_get failed for {key}: {e}")
        return None

def cache_set(key: str, value, ttl: int = 60):
    """Store in Redis with TTL in seconds. Silently fails."""
    client = get_redis()
    if client is None:
        return
    try:
        client.set(key, json.dumps(value), ex=ttl)
    except Exception as e:
        audit_log_sync("redis", "WARNING", f"cache_set failed for {key}: {e}")

def cache_delete(key: str):
    """Delete from Redis. Silently fails."""
    client = get_redis()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception as e:
        audit_log_sync("redis", "WARNING", f"cache_delete failed for {key}: {e}")

def redis_rate_limit_check(key: str, max_calls: int, window_seconds: int):
    """
    Distributed sliding window via Redis sorted set.
    Returns (allowed: bool, wait_secs: float)
    Returns None if Redis is unavailable (signal to fallback).
    """
    client = get_redis()
    if client is None:
        return None
        
    try:
        now = time.time()
        cutoff = now - window_seconds
        
        pipeline = client.pipeline()
        pipeline.zadd(key, {str(now): now})
        pipeline.zremrangebyscore(key, 0, cutoff)
        pipeline.expire(key, window_seconds)
        pipeline.zcard(key)
        pipeline.zrange(key, 0, 0, withscores=True)
        res = pipeline.exec()
        
        # res looks like [1, 0, True, count, [('member', score)]]
        if len(res) >= 5:
            count = int(res[3])
            if count > max_calls:
                oldest = res[4]
                if oldest and len(oldest) > 0:
                    if isinstance(oldest[0], tuple) or isinstance(oldest[0], list):
                        score = float(oldest[0][1])
                    else:
                        score = float(oldest[0])
                    wait = score + window_seconds - now
                    return (False, max(wait, 0.0))
                return (False, float(window_seconds))
                
        return (True, 0.0)
    except Exception as e:
        audit_log_sync("redis", "WARNING", f"rate_limit_check failed for {key}: {e}")
        return None
