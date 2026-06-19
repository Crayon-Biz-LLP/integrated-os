from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class Passage(BaseModel):
    source_type: str
    source_id: str
    memory_id: Optional[int] = None
    passage_index: int
    text: str
    char_count: int = 0
    source_fingerprint: Optional[str] = None
    index_version: int = 1
    metadata: dict = {}


class Triple(BaseModel):
    source_type: str
    source_id: str
    passage_id: Optional[int] = None
    subject_text: str
    predicate_text: str
    object_text: str
    normalized_subject: str
    normalized_predicate: str
    normalized_object: str
    confidence: float = 1.0
    extraction_model: Optional[str] = None
    index_version: int = 1


class PhraseNode(BaseModel):
    normalized_text: str
    display_text: str
    node_type: str = "concept"
    embedding: Optional[list] = None
    metadata: dict = {}


class PhraseNodeWithId(PhraseNode):
    id: int
    first_seen_at: datetime
    last_seen_at: datetime


class RetrievalEdge(BaseModel):
    from_node_id: int
    to_node_id: int
    edge_type: str = "related"
    weight: float = 1.0
    source_triple_id: Optional[int] = None
    source_passage_id: Optional[int] = None
    index_version: int = 1


class AliasEdge(BaseModel):
    from_node_id: int
    to_node_id: int
    alias_type: str = "heuristic"
    weight: float = 0.8


class PassagePhraseLink(BaseModel):
    passage_id: int
    node_id: int
    role: str = "mention"
    weight: float = 1.0


class MemoryBundleLink(BaseModel):
    memory_id: int
    passage_id: int
    index_version: int = 1


class NodeStats(BaseModel):
    node_id: int
    df: int = 0
    source_count: int = 0
    specificity_score: float = 0.5


class ScoredMemory(BaseModel):
    memory_id: int
    score: float
    passage_ids: List[int] = []
    supporting_passages: List[str] = []
    connected_phrases: List[str] = []
    explanation: str = ""


class ExplainableBundle(BaseModel):
    query: str
    items: List[ScoredMemory]
    total_candidates: int = 0
    latency_ms: int = 0
    debug_trace: Optional[dict] = None
    blended: bool = False


class IndexRun(BaseModel):
    id: Optional[int] = None
    source_type: str
    source_id: str
    source_fingerprint: Optional[str] = None
    index_version: int = 1
    status: str = "pending"
    error: Optional[str] = None
    retry_count: int = 0
