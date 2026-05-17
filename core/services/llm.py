import os
import json
import re
import time
import asyncio
import random
import httpx
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.lib.rate_limiter import flash_lite_limiter

_gemini_client = None

BRIEFING_MODEL = "gemini-3-flash-preview"
CLASSIFICATION_MODEL = "gemini-3.1-flash-lite-preview"
EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768
GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
GEMMA_SPEED_MODEL = "gemma-4-26b-a4b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"
PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")
PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini_client


class SimpleResponse:
    def __init__(self, text: str):
        self.text = text


def _jitter(delay: float) -> float:
    return delay * (0.75 + random.random() * 0.5)


async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents=None):
    if model is None:
        model = BRIEFING_MODEL
    gemini = get_gemini_client()
    max_retries = 5
    base_delay = 10

    for attempt in range(max_retries):
        try:
            if contents is not None:
                response = gemini.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config or {}
                )
            else:
                response = gemini.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config or {}
                )
            return response
        except Exception as e:
            error_str = str(e).lower()
            should_retry = any(err in error_str for err in RETRYABLE_ERRORS)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                audit_log_sync("llm", "WARNING", f"API Hiccup ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            raise


async def call_gemini_classify(prompt: str, model: str = None, config: dict = None, contents=None):
    if model is None:
        model = CLASSIFICATION_MODEL
    gemini = get_gemini_client()
    max_retries = 3
    base_delay = 1

    for attempt in range(max_retries):
        try:
            if "flash-lite" in model:
                await flash_lite_limiter.acquire_async()
            if contents is not None:
                response = gemini.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config or {}
                )
            else:
                response = gemini.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config or {}
                )
            return response
        except Exception as e:
            error_str = str(e).lower()
            retryable = ['503', '504', '500', 'timeout', 'deadline exceeded']
            should_retry = any(err in error_str for err in retryable)
            if should_retry and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                audit_log_sync("llm", "WARNING", f"Gemini error, retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(delay)
                continue
            raise


async def _call_openrouter(prompt: str, config: dict) -> SimpleResponse:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": PULSE_HTTP_REFERER,
        "X-Title": PULSE_APP_NAME
    }

    system_instruction = config.get('system_instruction') if config else None
    temperature = config.get('temperature', 0.7)
    response_mime_type = config.get('response_mime_type')

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature
    }

    if response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OPENROUTER_BASE_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if 'choices' in data and len(data['choices']) > 0:
            return SimpleResponse(text=data['choices'][0]['message']['content'])

        return SimpleResponse(text=data.get('content', '') or json.dumps(data))


async def call_llm_with_fallback(
    prompt: str,
    model: str = None,
    config: dict = None,
    contents=None,
    is_critical: bool = True,
    require_json: bool = False
):
    if model is None:
        model = BRIEFING_MODEL
    gemini = get_gemini_client()
    max_retries_per_provider = 3 if is_critical else 2
    base_delay = 10 if is_critical else 6

    providers = [
        {
            "provider": "gemini",
            "model": model,
            "fn": lambda p, c, cfg: gemini.models.generate_content(
                model=model,
                contents=c if c else p,
                config=cfg or {}
            )
        },
        {
            "provider": "gemma",
            "model": GEMMA_FALLBACK_MODEL,
            "fn": lambda p, c, cfg: gemini.models.generate_content(
                model=GEMMA_FALLBACK_MODEL,
                contents=c if c else p,
                config=cfg or {}
            )
        },
    ]

    if PULSE_ENABLE_OPENROUTER_FALLBACK and OPENROUTER_API_KEY:
        providers.append({
            "provider": "openrouter",
            "model": OPENROUTER_MODEL,
            "fn": lambda p, c, cfg: _call_openrouter(p, cfg or {})
        })

    last_error = None
    from core.services.db import get_supabase

    for provider_idx, prov in enumerate(providers):
        start_time = time.time()
        provider_name = prov["provider"]
        model_name = prov["model"]

        for attempt in range(max_retries_per_provider):
            try:
                if provider_name == "gemini" and "flash-lite" in model_name:
                    await flash_lite_limiter.acquire_async()
                response = prov["fn"](prompt, contents, config)
                elapsed = time.time() - start_time

                try:
                    supabase = get_supabase()
                    input_tokens = len(prompt) // 4 if prompt else 0
                    output_tokens = 0
                    if hasattr(response, 'text'):
                        output_tokens = len(response.text) // 4

                    supabase.table('model_registry').insert({
                        "model_name": model_name,
                        "provider": provider_name,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "latency_ms": int(elapsed * 1000),
                        "success": True
                    }).execute()
                except Exception:
                    pass

                if hasattr(response, 'text'):
                    response_text = response.text
                else:
                    response_text = str(response)

                if require_json:
                    try:
                        parse_json_response(response_text)
                    except ValueError as pe:
                        audit_log_sync("llm", "WARNING", f"LLM parse failed provider={provider_name} model={model_name}: {pe}")
                        if provider_idx == len(providers) - 1:
                            raise
                        continue

                return response

            except Exception as e:
                error_str = str(e).lower()
                elapsed = time.time() - start_time

                is_retryable = any(err in error_str for err in RETRYABLE_ERRORS)
                is_non_retryable = any(err in error_str for err in NON_RETRYABLE_ERRORS)

                if is_non_retryable:
                    audit_log_sync("llm", "ERROR", f"LLM non-retryable error provider={provider_name}: {e}")
                    raise

                if is_retryable and attempt < max_retries_per_provider - 1:
                    delay = _jitter(base_delay * (2 ** attempt))
                    audit_log_sync("llm", "WARNING",
                                  f"LLM retry provider={provider_name} model={model_name} attempt={attempt+1} delay={delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue

                last_error = e
                break

        if provider_idx < len(providers) - 1:
            pass

    raise last_error or Exception("All LLM providers failed")


def parse_json_response(response_text: str) -> any:
    if not response_text:
        raise ValueError("Empty response")

    text = response_text.strip()

    text = re.sub(r'^```json\n?', '', text)
    text = re.sub(r'\n?```$', '', text).strip()

    text = re.sub(r',\s*([}\]])', r'\1', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from response: {text[:100]}...")
