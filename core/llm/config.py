from dataclasses import dataclass
from typing import Literal

@dataclass
class LLMConfig:
    timeout_s: float
    max_retries: int
    limiter_mode: Literal["wait", "fail_fast", "consume_deadline", "none"] = "none"

class WorkloadProfile:
    INTERACTIVE = LLMConfig(timeout_s=15.0, max_retries=3, limiter_mode="consume_deadline")
    SYNTHESIS = LLMConfig(timeout_s=45.0, max_retries=4, limiter_mode="wait")
    BATCH = LLMConfig(timeout_s=120.0, max_retries=5, limiter_mode="wait")
    EMBEDDING = LLMConfig(timeout_s=10.0, max_retries=3, limiter_mode="consume_deadline")
