from core.lib.rate_limiter import SlidingWindowLimiter

def test_sliding_window_fallback():
    limiter = SlidingWindowLimiter(max_calls=5, per_seconds=60)
    
    # Should allow 5 calls with wait=0
    for _ in range(5):
        assert limiter._get_wait_secs() == 0.0
        limiter.acquire()
        
    # 6th call should require wait
    wait_time = limiter._get_wait_secs()
    assert wait_time > 0.0
