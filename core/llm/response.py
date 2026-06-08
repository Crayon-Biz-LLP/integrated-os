from dataclasses import dataclass
from typing import List, Optional, Any
import json
from .errors import ParseError

@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    workload: str
    success: bool
    degraded: bool
    degraded_reason: Optional[str]
    attempts: int
    latency_ms: int
    final_exception: Optional[Exception]

    def parse_json(self) -> Any:
        if not self.text:
            raise ParseError("Cannot parse empty response text")
        
        clean_text = self.text.replace('```json', '').replace('```', '').strip()
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError as e:
            raise ParseError(f"Failed to parse JSON: {e}") from e

@dataclass
class EmbeddingResult:
    vector: List[float]
    success: bool
    degraded: bool
    degraded_reason: Optional[str]
    provider: str
    model: str
    latency_ms: int
    
    @property
    def is_zero_vector(self) -> bool:
        return not bool(self.vector and any(self.vector))
