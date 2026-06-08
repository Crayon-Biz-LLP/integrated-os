from dataclasses import dataclass
from typing import List, Optional, Any
import json
import re
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
    function_calls: Optional[List[Any]] = None
    parsed_schema: Optional[Any] = None
    raw_response: Optional[Any] = None

    def parse_json(self) -> Any:
        if not self.text:
            raise ParseError("Cannot parse empty response text")
        
        text = self.text.strip()
        text = re.sub(r'^```json\n?', '', text)
        text = re.sub(r'\n?```$', '', text).strip()
        text = re.sub(r',\s*([}\]])', r'\1', text)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
            
        match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
                
        raise ParseError(f"Failed to parse JSON from response: {self.text[:100]}...")

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
