import time
import json
import asyncio
from typing import Any

from .config import WorkloadProfile, LLMConfig
from .response import LLMResponse
from .constants import Outcome, SAFE_HOLD_CLASSIFICATION
from .errors import DeadlineExceeded, NonRetryableError, BreakerOpenError
from .breaker import CircuitBreaker
from .retry import DeadlineBudget, get_jittered_backoff
from .providers import call_gemini, call_openrouter
from .instrument import log_llm_outcome

gemini_breaker = CircuitBreaker("gemini", threshold=4, window_s=60)

async def generate_content_with_fallback(
    prompt: str,
    workload: LLMConfig = WorkloadProfile.INTERACTIVE,
    primary_model: str = "gemini-3.1-flash-lite",
    fallback_model: str = "google/gemini-flash-1.5",
    contents: Any = None,
    is_classification: bool = False,
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
        log_llm_outcome(resp, outcome)
        return resp

    # Primary path (Gemini)
    if not gemini_breaker.is_open():
        for attempt in range(workload.max_retries):
            attempts += 1
            try:
                budget.check_deadline()
                
                # Enforce no-hop if budget is too low
                if not budget.has_budget_for_hop(1.0):
                    raise DeadlineExceeded("Insufficient budget for next attempt")
                    
                timeout_s = min(budget.time_remaining(), 15.0) # Cap individual hops
                
                text = await call_gemini(
                    model=primary_model,
                    prompt=prompt,
                    contents=contents,
                    timeout_s=timeout_s,
                    **kwargs
                )
                
                gemini_breaker.record_success()
                
                resp = LLMResponse(
                    text=text,
                    provider="gemini",
                    model=primary_model,
                    workload="classification" if is_classification else "general",
                    success=True,
                    degraded=False,
                    degraded_reason=None,
                    attempts=attempts,
                    latency_ms=int((time.time() - start_time) * 1000),
                    final_exception=None
                )
                
                outcome = Outcome.SUCCESS if attempt == 0 else Outcome.RETRY_SUCCESS
                log_llm_outcome(resp, outcome)
                return resp
                
            except DeadlineExceeded as e:
                final_exc = e
                return _create_degraded_response("deadline_exhausted", Outcome.DEADLINE_EXHAUSTED)
            except NonRetryableError as e:
                final_exc = e
                gemini_breaker.record_failure()
                break # Fall to secondary
            except Exception as e:
                final_exc = e
                gemini_breaker.record_failure()
                
                if attempt < workload.max_retries - 1:
                    try:
                        budget.check_deadline()
                        delay = get_jittered_backoff(attempt)
                        if not budget.has_budget_for_hop(delay):
                            raise DeadlineExceeded("Insufficient budget for backoff")
                        await asyncio.sleep(delay)
                    except DeadlineExceeded as de:
                        final_exc = de
                        return _create_degraded_response("deadline_exhausted", Outcome.DEADLINE_EXHAUSTED)
    else:
        final_exc = BreakerOpenError("Gemini breaker is open")

    # Fallback path (OpenRouter)
    if budget.has_budget_for_hop(1.0):
        attempts += 1
        try:
            timeout_s = min(budget.time_remaining(), 15.0)
            text = await call_openrouter(
                model=fallback_model,
                prompt=prompt,
                timeout_s=timeout_s,
                **kwargs
            )
            
            resp = LLMResponse(
                text=text,
                provider="openrouter",
                model=fallback_model,
                workload="classification" if is_classification else "general",
                success=True,
                degraded=False,
                degraded_reason=None,
                attempts=attempts,
                latency_ms=int((time.time() - start_time) * 1000),
                final_exception=None
            )
            log_llm_outcome(resp, Outcome.FALLBACK_SUCCESS)
            return resp
        except Exception as e:
            final_exc = e

    return _create_degraded_response("all_providers_failed", Outcome.SAFE_HOLD_EMITTED)
