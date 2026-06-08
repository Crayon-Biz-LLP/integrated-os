class LLMError(Exception):
    pass

class ProviderTimeout(LLMError):
    pass

class DeadlineExceeded(LLMError):
    pass

class BreakerOpenError(LLMError):
    pass

class ParseError(LLMError):
    pass

class NonRetryableError(LLMError):
    pass
