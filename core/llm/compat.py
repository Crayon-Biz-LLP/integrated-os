import asyncio
from typing import Any
from .fallback import generate_content_with_fallback
from .config import WorkloadProfile
from .constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL
from .embedding import get_embedding as _get_embedding_async

async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents: Any = None) -> Any:
    """Compat wrapper for existing call_gemini_with_retry consumers."""
    if model is None:
        model = CLASSIFICATION_MODEL
        
    is_classification = (model == CLASSIFICATION_MODEL)
    
    resp = await generate_content_with_fallback(
        prompt=prompt,
        workload=WorkloadProfile.INTERACTIVE,
        primary_model=model,
        contents=contents,
        is_classification=is_classification,
        config=config
    )
    
    class LegacyResponse:
        def __init__(self, text: str):
            self.text = text
            
    return LegacyResponse(resp.text)

async def call_llm_with_fallback(prompt: str, **kwargs) -> Any:
    """Compat wrapper for existing call_llm_with_fallback consumers."""
    resp = await generate_content_with_fallback(
        prompt=prompt,
        workload=WorkloadProfile.SYNTHESIS,
        primary_model=kwargs.pop('model', SYNTHESIS_MODEL),
        **kwargs
    )
    
    class LegacyResponse:
        def __init__(self, text: str, function_calls: Any = None):
            self.text = text
            if function_calls:
                self.function_calls = function_calls
            
    return LegacyResponse(resp.text, getattr(resp, 'function_calls', None))

async def get_embedding(text: str) -> list:
    """Compat wrapper for existing async get_embedding consumers."""
    resp = await _get_embedding_async(text)
    return resp.vector

def get_embedding_sync(text: str) -> list:
    """Compat wrapper for existing sync get_embedding consumers."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return asyncio.run(_get_embedding_async(text)).vector

    if loop.is_running():
        # Fallback for sync calls inside async context
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(_get_embedding_async(text)).vector
    else:
        return loop.run_until_complete(_get_embedding_async(text)).vector

def call_llm_with_fallback_sync(prompt: str, **kwargs) -> Any:
    """Sync wrapper for the async fallback chain, specifically for backfill_graph.py."""
    async def _run():
        is_critical = kwargs.pop('is_critical', True)
        workload = WorkloadProfile.INTERACTIVE if is_critical else WorkloadProfile.SYNTHESIS
        
        return await generate_content_with_fallback(
            prompt=prompt,
            workload=workload,
            primary_model=kwargs.pop('model', CLASSIFICATION_MODEL),
            **kwargs
        )
        
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        resp = asyncio.run(_run())
        class LegacyResponse:
            def __init__(self, text: str):
                self.text = text
        return LegacyResponse(resp.text)

    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        resp = asyncio.run(_run())
    else:
        resp = loop.run_until_complete(_run())
        
    class LegacyResponse:
        def __init__(self, text: str):
            self.text = text
    return LegacyResponse(resp.text)
