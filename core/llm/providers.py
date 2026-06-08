import os
import asyncio
from typing import Any
from httpx import AsyncClient
from .client import get_gemini_client
from .errors import ProviderTimeout, NonRetryableError
from core.lib.rate_limiter import flash_lite_limiter

async def call_gemini(model: str, prompt: str, contents: Any = None, timeout_s: float = 120.0, **kwargs) -> str:
    """Make a call to Gemini, enforcing the timeout via asyncio.wait_for"""
    if "flash-lite" in model:
        await flash_lite_limiter.acquire_async()
        
    try:
        def _call():
            if contents is not None:
                return get_gemini_client().models.generate_content(
                    model=model,
                    contents=contents,
                    config=kwargs.get('config')
                )
            else:
                return get_gemini_client().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=kwargs.get('config')
                )
        
        response = await asyncio.wait_for(
            asyncio.to_thread(_call),
            timeout=timeout_s
        )
        return response.text
    except asyncio.TimeoutError:
        raise ProviderTimeout(f"Gemini call timed out after {timeout_s}s")
    except Exception as e:
        error_str = str(e).lower()
        if any(err in error_str for err in ['503', '504', '500', 'timeout', 'deadline exceeded', '429']):
            raise  # Retryable
        else:
            raise NonRetryableError(f"Gemini non-retryable error: {e}") from e

async def call_openrouter(model: str, prompt: str, timeout_s: float = 120.0, **kwargs) -> str:
    """Fallback OpenRouter call"""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise NonRetryableError("OPENROUTER_API_KEY not configured")
        
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    url = f"{base_url}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000"),
        "X-Title": os.getenv("PULSE_APP_NAME", "Pulse"),
    }
    
    config = kwargs.get('config', {})
    is_json = config.get('response_mime_type') == 'application/json'
    
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    if is_json:
        payload["response_format"] = {"type": "json_object"}
        
    try:
        async with AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                return data['choices'][0]['message']['content']
            raise NonRetryableError("Invalid response format from OpenRouter")
            
    except asyncio.TimeoutError:
        raise ProviderTimeout(f"OpenRouter call timed out after {timeout_s}s")
    except Exception as e:
        error_str = str(e).lower()
        if any(err in error_str for err in ['503', '504', '500', 'timeout', '429']):
            raise  # Retryable
        else:
            raise NonRetryableError(f"OpenRouter non-retryable error: {e}") from e
