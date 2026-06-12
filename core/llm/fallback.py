import time
import json
import asyncio
from typing import Any

from .config import WorkloadProfile, LLMConfig
from .response import LLMResponse
from .constants import Outcome, SAFE_HOLD_CLASSIFICATION
from .errors import DeadlineExceeded, NonRetryableError, BreakerOpenError, ParseError
from .breaker import CircuitBreaker
from .retry import DeadlineBudget, get_jittered_backoff
from .providers import call_gemini, call_openrouter
from .instrument import log_llm_outcome
from core.lib.audit_logger import audit_log_sync

gemini_breaker = CircuitBreaker("gemini", threshold=4, window_s=60)

async def generate_content_with_fallback(
    prompt: str,
    workload: LLMConfig = WorkloadProfile.INTERACTIVE,
    primary_model: str = "gemini-3.5-flash",
    fallback_model: str = "nvidia/nemotron-3-super-120b-a12b:free",
    contents: Any = None,
    is_classification: bool = False,
    require_json: bool = False,
    schema: Any = None,
    **kwargs
) -> LLMResponse:
    
    start_time = time.time()
    budget = DeadlineBudget(workload)
    attempts = 0
    final_exc = None
    
    def _create_degraded_response(reason: str, outcome: Outcome, exc: Exception = None) -> LLMResponse:
        text = json.dumps(SAFE_HOLD_CLASSIFICATION) if is_classification else ""
        resp = LLMResponse(
            text=text,
            provider="fallback_chain",
            model="none",
            workload="classification" if is_classification else "general",
            success=False,
            degraded=True,
            degraded_reason=reason,
            attempts=attempts,
            latency_ms=int((time.time() - start_time) * 1000),
            final_exception=exc or final_exc
        )
        log_llm_outcome(resp, outcome, prompt=prompt)
        return resp
        
    async def _try_provider(provider_name, provider_fn, model_name, max_retries):
        nonlocal attempts, final_exc
        mutation_hint = ""
        
        for attempt in range(max_retries):
            attempts += 1
            try:
                budget.check_deadline()
                
                if not budget.has_budget_for_hop(1.0):
                    raise DeadlineExceeded("Insufficient budget for next attempt")
                    
                timeout_s = budget.time_remaining()
                
                current_prompt = prompt
                current_contents = contents
                
                if mutation_hint:
                    hint_text = f"\n\nSystem Correction for this attempt:\n{mutation_hint}"
                    if current_contents is None:
                        current_prompt += hint_text
                    elif isinstance(current_contents, list):
                        current_contents = current_contents + [hint_text]
                    elif isinstance(current_contents, str):
                        current_contents += hint_text
                        
                # Apply rate limiter for flash-lite
                if model_name == "gemini-3.1-flash-lite":
                    from core.lib.rate_limiter import flash_lite_limiter
                    await flash_lite_limiter.acquire_async()
                    # Re-check deadline after potentially waiting for rate limit
                    budget.check_deadline()
                    timeout_s = budget.time_remaining()
                
                text, function_calls, raw_response = await provider_fn(
                    model=model_name,
                    prompt=current_prompt,
                    contents=current_contents,
                    timeout_s=timeout_s,
                    **kwargs
                )
                
                parsed_schema = None
                if not function_calls and (require_json or schema):
                    try:
                        dummy_resp = LLMResponse(text=text, provider="", model="", workload="", success=True, degraded=False, degraded_reason=None, attempts=0, latency_ms=0, final_exception=None)
                        parsed = dummy_resp.parse_json()
                        if schema:
                            if hasattr(schema, 'model_validate'):
                                parsed_schema = schema.model_validate(parsed)
                            else:
                                parsed_schema = schema.parse_obj(parsed)
                    except Exception as ve:
                        if attempt < max_retries - 1:
                            mutation_hint = f"Your previous response failed validation: {str(ve)}. Please correct this and ensure you return ONLY valid JSON matching the schema."
                            delay = get_jittered_backoff(attempt)
                            if not budget.has_budget_for_hop(delay):
                                raise DeadlineExceeded("Insufficient budget for backoff")
                            audit_log_sync("llm", "WARNING", f"⚠️ LLM retry (validation) provider={provider_name} attempt={attempt+1} error={str(ve)[:50]}")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            raise ParseError(f"Validation failed after all retries: {ve}") from ve
                            
                resp = LLMResponse(
                    text=text,
                    provider=provider_name,
                    model=model_name,
                    workload="classification" if is_classification else "general",
                    success=True,
                    degraded=False,
                    degraded_reason=None,
                    attempts=attempts,
                    latency_ms=int((time.time() - start_time) * 1000),
                    final_exception=None,
                    function_calls=function_calls,
                    parsed_schema=parsed_schema,
                    raw_response=raw_response
                )
                return resp
                
            except DeadlineExceeded as e:
                final_exc = e
                raise
            except NonRetryableError as e:
                final_exc = e
                raise
            except ParseError as e:
                final_exc = e
                raise
            except Exception as e:
                final_exc = e
                if attempt < max_retries - 1:
                    try:
                        budget.check_deadline()
                        delay = get_jittered_backoff(attempt)
                        if not budget.has_budget_for_hop(delay):
                            raise DeadlineExceeded("Insufficient budget for backoff")
                        await asyncio.sleep(delay)
                    except DeadlineExceeded as de:
                        final_exc = de
                        raise
                else:
                    raise

    # Primary path (Gemini)
    if not gemini_breaker.is_open():
        try:
            resp = await _try_provider("gemini", call_gemini, primary_model, workload.max_retries)
            gemini_breaker.record_success()
            outcome = Outcome.SUCCESS if resp.attempts == 1 else Outcome.RETRY_SUCCESS
            log_llm_outcome(resp, outcome, prompt=prompt)
            return resp
        except (DeadlineExceeded, NonRetryableError, ParseError):
            gemini_breaker.record_failure()
        except Exception:
            gemini_breaker.record_failure()
    else:
        final_exc = BreakerOpenError("Gemini breaker is open")

    # Fallback path 1 (Gemma via Gemini SDK)
    if budget.has_budget_for_hop(1.0):
        try:
            resp = await _try_provider("gemini_gemma", call_gemini, "gemma-4-31b-it", 1)
            log_llm_outcome(resp, Outcome.FALLBACK_SUCCESS, prompt=prompt)
            return resp
        except Exception as e:
            final_exc = e

    # Fallback path 2 (OpenRouter)
    if budget.has_budget_for_hop(1.0):
        try:
            resp = await _try_provider("openrouter", call_openrouter, fallback_model, 1)
            log_llm_outcome(resp, Outcome.FALLBACK_SUCCESS, prompt=prompt)
            return resp
        except Exception as e:
            final_exc = e

    # Determine degraded reason
    reason = "all_providers_failed"
    if isinstance(final_exc, DeadlineExceeded):
        reason = "deadline_exhausted"
        outcome = Outcome.DEADLINE_EXHAUSTED
    else:
        outcome = Outcome.SAFE_HOLD_EMITTED

    return _create_degraded_response(reason, outcome)
