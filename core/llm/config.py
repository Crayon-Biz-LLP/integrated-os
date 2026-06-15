from dataclasses import dataclass
from typing import Literal

@dataclass
class LLMConfig:
    timeout_s: float
    max_retries: int
    limiter_mode: Literal["wait", "fail_fast", "consume_deadline", "none"] = "none"

class WorkloadProfile:
    INTERACTIVE = LLMConfig(timeout_s=55.0, max_retries=3, limiter_mode="consume_deadline")
    SYNTHESIS = LLMConfig(timeout_s=300.0, max_retries=4, limiter_mode="wait")
    BATCH = LLMConfig(timeout_s=300.0, max_retries=5, limiter_mode="wait")
    EMBEDDING = LLMConfig(timeout_s=120.0, max_retries=3, limiter_mode="consume_deadline")

# Reduced 15% from nominal to account for prompt boilerplate
CONTEXT_TOKEN_BUDGETS = {
    'morning_pulse':    1700,
    'email_triage':      340,
    'capture_grounding': 680,
}
