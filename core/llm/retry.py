import time
import random
from typing import TypeVar
from .config import LLMConfig
from .errors import DeadlineExceeded

T = TypeVar('T')

class DeadlineBudget:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.start_time = time.time()
        self.deadline = self.start_time + config.timeout_s
        
    def time_remaining(self) -> float:
        return max(0.0, self.deadline - time.time())
        
    def check_deadline(self) -> None:
        if self.time_remaining() <= 0:
            raise DeadlineExceeded("Overall deadline budget exhausted")

    def has_budget_for_hop(self, hop_estimated_s: float = 1.0) -> bool:
        return self.time_remaining() >= hop_estimated_s

def get_jittered_backoff(attempt: int, base_delay: float = 2.0) -> float:
    # Exponential backoff with jitter
    delay = base_delay * (2 ** attempt)
    jitter = random.uniform(0, 0.5 * delay)
    return delay + jitter
