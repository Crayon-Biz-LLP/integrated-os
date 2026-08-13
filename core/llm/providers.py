import os
import asyncio
from typing import Any, Tuple, List, Optional
from httpx import AsyncClient
from .client import get_gemini_clients
from .errors import ProviderTimeout, NonRetryableError
from core.lib.rate_limiter import flash_lite_limiter, flash_3_5_limiter

def _schema_rejection(e: Exception) -> bool:
    """True if the error looks like a response_schema rejection.

    Phase 5 degradation: if a provider rejects the schema (invalid/unsupported),
    the caller retries without it rather than failing the whole call.
    """
    msg = str(e).lower()
    return (
        "response_schema" in msg
        or "invalid_json_schema" in msg
        or "invalid json schema" in msg
        or ("invalid argument" in msg and "schema" in msg)
    )


def openrouter_response_format(config: dict) -> Optional[dict]:
    """Map the internal `config` to OpenRouter's `response_format` (Phase 5).

    - `response_schema` present → json_schema (structured output)
    - else `response_mime_type == application/json` → json_object
    - else None (free text)
    """
    if not config:
        return None
    schema = config.get("response_schema")
    if schema:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_response",
                "strict": False,
                "schema": schema,
            },
        }
    if config.get("response_mime_type") == "application/json":
        return {"type": "json_object"}
    return None


async def call_gemini(model: str, prompt: str, contents: Any = None, timeout_s: float = 120.0, limiter: Any = None, **kwargs) -> Tuple[str, Optional[List[Any]], Any]:
    """Make a call to Gemini, enforcing the timeout via asyncio.wait_for. Supports multi-key failover.

    `limiter` overrides the auto-selected pool (e.g. a workload-dedicated
    limiter like the sentinel's) so distinct workloads never share a window
    and cannot starve each other.
    """
    clients = get_gemini_clients()
    
    if limiter is not None:
        client_idx = await limiter.acquire_async()
        clients = clients[client_idx:] + clients[:client_idx]
    elif "flash-lite" in model:
        client_idx = await flash_lite_limiter.acquire_async()
        clients = clients[client_idx:] + clients[:client_idx]
    elif "flash" in model:
        client_idx = await flash_3_5_limiter.acquire_async()
        clients = clients[client_idx:] + clients[:client_idx]
        
    last_error = None
    
    for client in clients:
        call_config = kwargs.get('config')
        schema_dropped = False
        while True:
            try:
                def _call():
                    if contents is not None:
                        return client.models.generate_content(
                            model=model,
                            contents=contents,
                            config=call_config
                        )
                    else:
                        return client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=call_config
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
                # Phase 5 degradation: the response_schema was rejected — retry
                # this client once without it rather than failing the call.
                if not schema_dropped and _schema_rejection(e) and call_config and "response_schema" in call_config:
                    schema_dropped = True
                    call_config = {k: v for k, v in call_config.items() if k != "response_schema"}
                    continue
                if any(err in error_str for err in ['429', 'resource_exhausted', 'quota']):
                    last_error = e
                    break  # try next client
                
                if any(err in error_str for err in ['503', '504', '500', 'timeout', 'timed out', 'deadline exceeded']):
                    raise  # Retryable (fallback chain will handle it)
                else:
                    raise NonRetryableError(f"Gemini non-retryable error: {e}") from e

    # If we get here, all clients hit a quota error
    if last_error is None:
        raise RuntimeError("All Gemini clients exhausted without catching a quota or timeout error")
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
    
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    response_format = openrouter_response_format(config)
    if response_format:
        payload["response_format"] = response_format
        
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
