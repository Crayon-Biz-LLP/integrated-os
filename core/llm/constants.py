from enum import Enum

class Outcome(str, Enum):
    SUCCESS = "success"
    RETRY_SUCCESS = "retry_success"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    DEADLINE_EXHAUSTED_WAITING_FOR_LIMITER = "deadline_exhausted_waiting_for_limiter"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_5XX = "provider_5xx"
    BREAKER_OPEN = "breaker_open"
    FALLBACK_SUCCESS = "fallback_success"
    SAFE_HOLD_EMITTED = "safe_hold_emitted"
    EMBEDDING_ZERO_VECTOR_FALLBACK = "embedding_zero_vector_fallback"
    NON_RETRYABLE_ERROR = "non_retryable_error"
    PARSE_ERROR = "parse_error"

SAFE_HOLD_CLASSIFICATION = {
    "intent": "NOTE",
    "confidence": 1.0,
    "entity": "INBOX",
    "title": "Fallback Note",
    "time_context": "",
    "clarification_question": "",
    "receipt": "Message vaulted safely (AI classification temporarily unavailable).",
    "possible_intents": [],
    "reasoning": "safe_hold"
}

CLASSIFICATION_MODEL = "gemini-3.5-flash-lite"
SYNTHESIS_MODEL = "gemini-3.6-flash"
EMBEDDING_MODEL = "gemini-embedding-2"
GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

EMBEDDING_DIMENSION = 768

RETRYABLE_ERRORS = [
    '503', '504', '500', 'disconnected', 'timeout', 
    'deadline exceeded', 'unavailable', 'overloaded', 'rate limit'
]

NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']
