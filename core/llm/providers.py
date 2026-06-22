import os
import asyncio
from typing import Any, Tuple, List, Optional
from httpx import AsyncClient
from .client import get_gemini_clients
from .errors import ProviderTimeout, NonRetryableError
from core.lib.rate_limiter import flash_lite_limiter, flash_3_5_limiter

async def call_gemini(model: str, prompt: str, contents: Any = None, timeout_s: float = 120.0, **kwargs) -> Tuple[str, Optional[List[Any]], Any]:
    """Make a call to Gemini, enforcing the timeout via asyncio.wait_for. Supports multi-key failover."""
    clients = get_gemini_clients()
    
    if "flash-lite" in model:
        client_idx = await flash_lite_limiter.acquire_async()
        clients = clients[client_idx:] + clients[:client_idx]
    elif "flash" in model:
        client_idx = await flash_3_5_limiter.acquire_async()
        clients = clients[client_idx:] + clients[:client_idx]
        
    last_error = None
    
    for client in clients:
        try:
            def _call():
                if contents is not None:
                    return client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=kwargs.get('config')
                    )
                else:
                    return client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=kwargs.get('config')
                    )
            
            timeout_val = min(timeout_s, 180.0)
            response = await asyncio.wait_for(
                asyncio.to_thread(_call),
                timeout=timeout_val
            )
            
            response_text = ""
            try:
                if hasattr(response, 'text') and response.text:
                    response_text = response.text
            except ValueError:
                pass
                
            function_calls = getattr(response, 'function_calls', None)
            return response_text, function_calls, response
            
        except asyncio.TimeoutError as e:
            # Timeout applies to the whole function, not per-client, but if it times out, 
            # we should raise it rather than trying another client
            raise ProviderTimeout(f"Gemini call timed out after {timeout_val}s") from e
        except Exception as e:
            error_str = str(e).lower()
            if any(err in error_str for err in ['429', 'resource_exhausted', 'quota']):
                last_error = e
                continue # Try next client
            
            if any(err in error_str for err in ['503', '504', '500', 'timeout', 'timed out', 'deadline exceeded']):
                raise  # Retryable (fallback chain will handle it)
            else:
                raise NonRetryableError(f"Gemini non-retryable error: {e}") from e

    # If we get here, all clients hit a quota error
    raise last_error

async def call_openrouter(model: str, prompt: str, timeout_s: float = 120.0, **kwargs) -> Tuple[str, Optional[List[Any]], Any]:
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
                return data['choices'][0]['message']['content'], None, data
            raise NonRetryableError("Invalid response format from OpenRouter")
            
    except asyncio.TimeoutError:
        raise ProviderTimeout(f"OpenRouter call timed out after {timeout_s}s")
    except Exception as e:
        error_str = str(e).lower()
        if any(err in error_str for err in ['503', '504', '500', 'timeout', 'timed out', '429']):
            raise  # Retryable
        else:
            raise NonRetryableError(f"OpenRouter non-retryable error: {e}") from e
