"""Streaming LLM provider with fallback.

Attempts Gemini native streaming first. If that fails (timeout, safety filter,
breaker open), falls back to the existing non-streaming generate_content_with_fallback
and yields the entire response as a single token.

Usage:
    async for token in stream_with_fallback(prompt, ...):
        # token is a string chunk of the response
        await adapter.send_chunk(token)
"""

from typing import AsyncGenerator

from core.llm.client import get_gemini_clients
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.llm.constants import SYNTHESIS_MODEL
from core.lib.audit_logger import audit_log_sync


async def stream_with_fallback(
    prompt: str,
    workload: WorkloadProfile = WorkloadProfile.INTERACTIVE,
    primary_model: str = SYNTHESIS_MODEL,
) -> AsyncGenerator[str, None]:
    """Stream Gemini response token by token.
    
    Tries native streaming with the primary Gemini model.
    Falls back to non-streaming generate_content_with_fallback on any error,
    yielding the full response as one chunk.
    """
    clients = get_gemini_clients()
    if not clients:
        audit_log_sync("llm", "WARNING", "stream_with_fallback: no Gemini clients available, falling back")
        async for token in _fallback_nonstreaming(prompt, workload, primary_model):
            yield token
        return

    # Try each Gemini client in sequence
    for client_idx, client in enumerate(clients):
        try:
            stream = await client.aio.models.generate_content_stream(
                model=primary_model,
                contents=prompt,
                config={
                    "max_output_tokens": 800,
                },
            )
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
            # Stream completed successfully — return
            return

        except Exception as e:
            audit_log_sync(
                "llm", "WARNING",
                f"stream_with_fallback: client {client_idx} failed: {e}. "
                f"{'Trying next client...' if client_idx < len(clients) - 1 else 'Falling back to non-streaming...'}"
            )
            # Continue to next client or fallback

    # All clients failed — fall back to non-streaming
    async for token in _fallback_nonstreaming(prompt, workload, primary_model):
        yield token


async def _fallback_nonstreaming(
    prompt: str,
    workload: WorkloadProfile,
    model: str,
) -> AsyncGenerator[str, None]:
    """Fall back to non-streaming generate_content_with_fallback.
    Yields the full response as a single token.
    """
    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=workload,
            primary_model=model,
        )
        if response and response.text:
            yield response.text
    except Exception as e:
        audit_log_sync("llm", "ERROR", f"stream_with_fallback: non-streaming fallback also failed: {e}")
        yield ""
