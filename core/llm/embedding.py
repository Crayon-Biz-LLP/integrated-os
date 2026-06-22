import time
import asyncio
import hashlib
from collections import OrderedDict
from .response import EmbeddingResult
from .constants import Outcome, EMBEDDING_MODEL
from .instrument import log_embedding_outcome
from .config import WorkloadProfile
from .retry import get_jittered_backoff
from core.lib.audit_logger import audit_log_sync

EMBEDDING_DIMENSION = 768

# LRU Cache for embeddings (process lifetime)
_EMBEDDING_CACHE = OrderedDict()
_MAX_CACHE_SIZE = 500

async def get_embedding(text: str) -> EmbeddingResult:
    if not text or not text.strip():
        return EmbeddingResult(vector=[0.0] * EMBEDDING_DIMENSION, success=False, degraded=True, degraded_reason="empty_text", provider="none", model="none", latency_ms=0)
        
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
    if text_hash in _EMBEDDING_CACHE:
        # Move to end to maintain LRU order
        cached_resp = _EMBEDDING_CACHE.pop(text_hash)
        _EMBEDDING_CACHE[text_hash] = cached_resp
        return cached_resp

    start_time = time.time()
    workload = WorkloadProfile.EMBEDDING
    max_retries = 3
    
    from core.lib.rate_limiter import embedding_limiter
    
    def _call(c_idx: int):
        from .client import get_gemini_clients
        clients = get_gemini_clients()
        if clients:
            clients = clients[c_idx:] + clients[:c_idx]
            
        last_error = None
        for client in clients:
            try:
                return client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=text,
                    config={
                        'output_dimensionality': EMBEDDING_DIMENSION
                    }
                )
            except Exception as e:
                error_str = str(e).lower()
                if '429' in error_str or 'resource_exhausted' in error_str or 'quota' in error_str:
                    last_error = e
                    continue
                raise e
        
        if last_error:
            raise last_error
        raise RuntimeError("No Gemini clients available")
        
    for attempt in range(max_retries):
        try:
            client_idx = await embedding_limiter.acquire_async()
            result = await asyncio.wait_for(
                asyncio.to_thread(_call, client_idx),
                timeout=workload.timeout_s
            )
            
            vector = result.embeddings[0].values
            
            resp = EmbeddingResult(
                vector=vector,
                success=True,
                degraded=False,
                degraded_reason=None,
                provider="gemini",
                model=EMBEDDING_MODEL,
                latency_ms=int((time.time() - start_time) * 1000)
            )
            
            # Record successful retry if not first attempt
            if attempt > 0:
                log_embedding_outcome(resp, Outcome.RETRY_SUCCESS)
            else:
                log_embedding_outcome(resp, Outcome.SUCCESS)
                
            # Cache the successful result
            _EMBEDDING_CACHE[text_hash] = resp
            if len(_EMBEDDING_CACHE) > _MAX_CACHE_SIZE:
                _EMBEDDING_CACHE.popitem(last=False)
                
            return resp
            
        except Exception as e:
            is_timeout = isinstance(e, asyncio.TimeoutError) or isinstance(e, TimeoutError)
            error_str = str(e).lower()
            is_rate_limit = '429' in error_str or 'resource exhausted' in error_str or 'quota' in error_str or 'timeout' in error_str
            is_ssl_or_connection = (
                'ssl' in error_str
                or 'wrong_version_number' in error_str
                or 'decryption_failed' in error_str
                or 'bad_record_mac' in error_str
                or 'server disconnected' in error_str
                or 'client has been closed' in error_str
            )
            is_retryable = is_rate_limit or is_timeout or is_ssl_or_connection
            
            error_desc = "asyncio.TimeoutError" if is_timeout else str(e)
            
            # Invalidate the cached client on SSL/connection errors so
            # the next attempt creates a fresh httpx connection pool.
            if is_ssl_or_connection:
                import core.llm.client as _client_mod
                _client_mod._gemini_client = None
            
            if attempt < max_retries - 1 and is_retryable:
                delay = get_jittered_backoff(attempt)
                audit_log_sync("llm", "WARNING", f"Embedding retry (attempt {attempt+1}/{max_retries}), retrying in {delay:.1f}s... {error_desc}")
                await asyncio.sleep(delay)
                continue
                
            resp = EmbeddingResult(
                vector=[0.0] * EMBEDDING_DIMENSION,
                success=False,
                degraded=True,
                degraded_reason=f"gemini_embedding_failed: {error_desc}",
                provider="fallback_chain",
                model="none",
                latency_ms=int((time.time() - start_time) * 1000)
            )
            log_embedding_outcome(resp, Outcome.EMBEDDING_ZERO_VECTOR_FALLBACK)
            return resp
