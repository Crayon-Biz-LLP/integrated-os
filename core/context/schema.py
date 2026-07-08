from dataclasses import dataclass
from typing import List, Dict, Any, Literal

@dataclass
class RetrievalItem:
    item_id: str
    content: str
    metadata: Dict[str, Any]
    score: float
    source: str  # "tasks", "people", "emails", "memories", "meeting_minutes"
    
@dataclass
class GateDecision:
    gate_name: str
    action: Literal["keep", "neutral_keep", "grounded_keep", "reject", "downrank"]
    reason: str
    item_id: str

@dataclass
class ContextResult:
    matched_items: List[RetrievalItem]
    excluded_items: List[RetrievalItem]
    exclusion_reasons: Dict[str, str]  # item_id -> reason
    gate_decisions: List[GateDecision]
    ranking_features_used: List[str]
    
    def get_formatted_context(self) -> str:
        """Format matched items for prompt ingestion."""
        parts = []
        for item in self.matched_items:
            prefix = f"[{item.source.upper()}]" if item.source != "memories" else f"[{item.metadata.get('memory_type', 'MEMORY').upper()}]"
            parts.append(f"{prefix} {item.content}")
        return "\n".join(parts)

