import time
import asyncio
from .client import get_gemini_client
from .response import EmbeddingResult
from .constants import Outcome, EMBEDDING_MODEL
from .instrument import log_embedding_outcome
from .config import WorkloadProfile
from .retry import get_jittered_backoff
from core.lib.audit_logger import audit_log_sync

EMBEDDING_DIMENSION = 768

async def get_embedding(text: str) -> EmbeddingResult:
    start_time = time.time()
    workload = WorkloadProfile.EMBEDDING
    max_retries = 3
    
    def _call():
        return get_gemini_client().models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        
    for attempt in range(max_retries):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_call),
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
                
            return resp
            
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = '429' in error_str or 'resource exhausted' in error_str or 'quota' in error_str or 'timeout' in error_str
            
            if attempt < max_retries - 1 and is_rate_limit:
                delay = get_jittered_backoff(attempt)
                audit_log_sync("llm", "WARNING", f"Embedding rate limit/timeout (attempt {attempt+1}/{max_retries}), retrying in {delay:.1f}s... {e}")
                await asyncio.sleep(delay)
                continue
                
            resp = EmbeddingResult(
                vector=[0.0] * EMBEDDING_DIMENSION,
                success=False,
                degraded=True,
                degraded_reason=f"gemini_embedding_failed: {e}",
                provider="fallback_chain",
                model="none",
                latency_ms=int((time.time() - start_time) * 1000)
            )
            log_embedding_outcome(resp, Outcome.EMBEDDING_ZERO_VECTOR_FALLBACK)
            return resp
