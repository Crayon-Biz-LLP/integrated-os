from core.lib.audit_logger import audit_log_sync
from .constants import Outcome
from .response import LLMResponse, EmbeddingResult

def log_llm_outcome(response: LLMResponse, outcome: Outcome):
    status = "WARNING" if response.degraded else "INFO"
    if not response.success and not response.degraded:
        status = "ERROR"
        
    msg = f"LLM[{response.provider}:{response.model}] {outcome.value} " \
          f"({response.latency_ms}ms, {response.attempts} attempts)"
    
    if response.degraded_reason:
        msg += f" reason: {response.degraded_reason}"
        
    audit_log_sync("llm", status, msg)

def log_embedding_outcome(result: EmbeddingResult, outcome: Outcome):
    status = "WARNING" if result.degraded else "INFO"
    if not result.success and not result.degraded:
        status = "ERROR"
        
    msg = f"Embed[{result.provider}:{result.model}] {outcome.value} " \
          f"({result.latency_ms}ms)"
          
    if result.degraded_reason:
        msg += f" reason: {result.degraded_reason}"
        
    audit_log_sync("llm_embedding", status, msg)
