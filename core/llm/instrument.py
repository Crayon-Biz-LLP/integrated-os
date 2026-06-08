import os
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from .constants import Outcome
from .response import LLMResponse, EmbeddingResult

_supabase = None
def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.getenv("SUPABASE_URL", "http://localhost"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "dummy")
        )
    return _supabase

def log_llm_outcome(response: LLMResponse, outcome: Outcome, prompt: str = ""):
    status = "WARNING" if response.degraded else "INFO"
    if not response.success and not response.degraded:
        status = "ERROR"
        
    msg = f"LLM[{response.provider}:{response.model}] {outcome.value} " \
          f"({response.latency_ms}ms, {response.attempts} attempts)"
    
    if response.degraded_reason:
        msg += f" reason: {response.degraded_reason}"
        
    audit_log_sync("llm", status, msg)
    
    # Log to model_registry if successful
    if response.success and not response.degraded:
        try:
            input_tokens = len(str(prompt)) // 4 if prompt else 0
            output_tokens = len(str(response.text)) // 4 if response.text else 0
            if response.function_calls:
                output_tokens += len(str(response.function_calls)) // 4
                
            get_supabase().table('model_registry').insert({
                "model_name": response.model,
                "provider": response.provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": response.latency_ms,
                "success": True
            }).execute()
        except Exception as e:
            audit_log_sync("llm", "WARNING", f"Failed to log to model_registry: {e}")

def log_embedding_outcome(result: EmbeddingResult, outcome: Outcome):
    status = "WARNING" if result.degraded else "INFO"
    if not result.success and not result.degraded:
        status = "ERROR"
        
    msg = f"Embed[{result.provider}:{result.model}] {outcome.value} " \
          f"({result.latency_ms}ms)"
          
    if result.degraded_reason:
        msg += f" reason: {result.degraded_reason}"
        
    audit_log_sync("llm_embedding", status, msg)
