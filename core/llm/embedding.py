import time
import asyncio
from .client import get_gemini_client
from .response import EmbeddingResult
from .constants import Outcome, EMBEDDING_MODEL
from .instrument import log_embedding_outcome
from .config import WorkloadProfile

EMBEDDING_DIMENSION = 768

async def get_embedding(text: str) -> EmbeddingResult:
    start_time = time.time()
    workload = WorkloadProfile.EMBEDDING
    
    try:
        def _call():
            return get_gemini_client().models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config={
                    'output_dimensionality': EMBEDDING_DIMENSION
                }
            )
            
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
        log_embedding_outcome(resp, Outcome.SUCCESS)
        return resp
        
    except Exception as e:
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
