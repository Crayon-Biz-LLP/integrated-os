import os
import json
import re
import asyncio
import time
import random
import httpx
from supabase import create_client, Client
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.lib.rate_limiter import flash_lite_limiter

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


def is_already_in_email_queue(title: str) -> bool:
    """Check if a task title already exists in email_pending_tasks."""
    try:
        keywords = [w for w in title.lower().split() if len(w) > 4]
        if not keywords:
            return False
        for kw in keywords[:3]:
            result = supabase.table('email_pending_tasks')\
                .select('id')\
                .ilike('suggested_title', f'%{kw}%')\
                .is_('danny_decision', 'null')\
                .limit(1)\
                .execute()
            if result.data:
                audit_log_sync("pulse", "WARNING", f"⚠️  Duplicate guard: '{title}' matches pending email task (keyword: '{kw}'). Skipping.")
                return True

        # Semantic embedding check (high threshold to avoid false positives)
        embedding = get_embedding(title)
        similarity_res = supabase.rpc('match_memories', {
            'query_embedding': embedding,
            'match_count': 1,
            'match_threshold': 0.88
        }).execute()
        if similarity_res.data:
            score = similarity_res.data[0].get('similarity')
            if isinstance(score, (int, float)) and score > 0:
                audit_log_sync("pulse", "WARNING", f"⚠️ Semantic duplicate guard: '{title}' is semantically similar to an existing memory. Skipping.")
                return True

        return False
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Duplicate guard check failed: {e}")
        return False

async def call_gemini_with_retry(prompt: str, model: str = None, config: dict = None, contents=None):
    if model is None:
        model = BRIEFING_MODEL

    max_retries = 5
    base_delay = 10

    for attempt in range(max_retries):
        try:
            if contents is not None:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config or {}
                )
            else:
                response = gemini_client.models.generate_content(
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
                audit_log_sync("pulse", "WARNING", f"⚠️ API Hiccup ({error_str}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                raise

class SimpleResponse:
    """Simple response wrapper for OpenRouter responses."""
    def __init__(self, text: str):
        self.text = text

def _jitter(delay: float) -> float:
    """Add jitter to delay: +/- 25%"""
    return delay * (0.75 + random.random() * 0.5)

def parse_json_response(response_text: str) -> any:
    """Robust JSON parsing with extraction fallback."""
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

async def call_llm_with_fallback(
    prompt: str,
    model: str = None,
    config: dict = None,
    contents=None,
    is_critical: bool = True,
    require_json: bool = False
):
    """
    Multi-provider LLM call with fallback chain.

    Provider chain:
    1. Primary: Gemini (gemini-3-flash-preview)
    2. Fallback: Gemma (gemma-4-31b-it)
    3. Fallback: OpenRouter (nvidia/nemotron-3-super-120b-a12b:free)

    Args:
        prompt: The prompt to send
        model: Override primary model (default: BRIEFING_MODEL)
        config: Generation config (temperature, system instruction, etc.)
        contents: Multi-modal contents instead of text prompt
        is_critical: If false, use faster fallback for non-critical ops
        require_json: If true, ensure JSON output parsing

    Returns:
        Response object with .text attribute

    Raises:
        Exception if all providers fail
    """
    if model is None:
        model = BRIEFING_MODEL

    max_retries_per_provider = 3 if is_critical else 2
    base_delay = 10 if is_critical else 6

    providers = [
        {
            "provider": "gemini",
            "model": model,
            "fn": lambda p, c, cfg: gemini_client.models.generate_content(
                model=model,
                contents=c if c else p,
                config=cfg or {}
            )
        },
        {
            "provider": "gemma",
            "model": GEMMA_FALLBACK_MODEL,
            "fn": lambda p, c, cfg: gemini_client.models.generate_content(
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

    for provider_idx, prov in enumerate(providers):
        start_time = time.time()
        provider_name = prov["provider"]
        model_name = prov["model"]

        for attempt in range(max_retries_per_provider):
            try:
                # Rate limit: only for flash-lite model
                if provider_name == "gemini" and "flash-lite" in model_name:
                    await flash_lite_limiter.acquire_async()
                response = prov["fn"](prompt, contents, config)
                elapsed = time.time() - start_time

                # Log to model_registry
                try:
                    input_tokens = len(prompt) // 4 if prompt else 0  # Rough estimate
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
                except Exception as log_err:
                    pass  # Don't fail main flow if logging fails

                if hasattr(response, 'text'):
                    response_text = response.text
                else:
                    response_text = str(response)

                if require_json:
                    try:
                        parsed = parse_json_response(response_text)
                    except ValueError as pe:
                        audit_log_sync("pulse", "WARNING", f"⚠️ LLM parse failed provider={provider_name} model={model_name}: {pe}")
                        if provider_idx == len(providers) - 1:
                            raise
                        continue

                print(f"✓ LLM success provider={provider_name} model={model_name} elapsed={elapsed:.1f}s")
                return response

            except Exception as e:
                error_str = str(e).lower()
                elapsed = time.time() - start_time

                is_retryable = any(err in error_str for err in RETRYABLE_ERRORS)
                is_non_retryable = any(err in error_str for err in NON_RETRYABLE_ERRORS)

                if is_non_retryable:
                    audit_log_sync("pulse", "ERROR", f"✗ LLM non-retryable error provider={provider_name}: {e}")
                    raise

                if is_retryable and attempt < max_retries_per_provider - 1:
                    delay = _jitter(base_delay * (2 ** attempt))
                    audit_log_sync("pulse", "WARNING", f"⚠️ LLM retry provider={provider_name} model={model_name} attempt={attempt+1} delay={delay:.0f}s error={error_str[:50]}")
                    await asyncio.sleep(delay)
                    continue

                audit_log_sync("pulse", "WARNING", f"⚠️ LLM provider failed provider={provider_name} model={model_name}: {error_str[:80]}")
                last_error = e
                break

        if provider_idx < len(providers) - 1:
            print(f"🔄 LLM fallback -> {providers[provider_idx + 1]['provider']}")

    raise last_error or Exception("All LLM providers failed")

async def _call_openrouter(prompt: str, config: dict) -> any:
    """Call OpenRouter API."""
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

def get_embedding(text: str) -> list:
    """Generate embedding for text using gemini-embedding-2-preview."""
    try:
        # 🎯 FORCE 768 dimensions to match your Supabase schema
        result = gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                'output_dimensionality': EMBEDDING_DIMENSION
            }
        )
        return result.embeddings[0].values
    except Exception as e:
        # Fallback to zero-vector on error to prevent total system crash
        audit_log_sync("pulse", "ERROR", f"Embedding error: {e}")
        return [0] * EMBEDDING_DIMENSION

def cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"

PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "http://localhost:8000")

PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Pulse")

GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"

GEMMA_SPEED_MODEL = "gemma-4-26b-a4b-it"

OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']

NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

EMBEDDING_MODEL = "gemini-embedding-2-preview"

EMBEDDING_DIMENSION = 768

BRIEFING_MODEL = "gemini-3-flash-preview"
