import os
from core.llm.constants import CLASSIFICATION_MODEL


class RetrievalConfig:
    """Feature flags for the retrieval pipeline. All default OFF.
    
    Rollout order:
    1. indexing_enabled + historical backfill
    2. shadow_mode for side-by-side comparison
    3. associative_enabled for selective query use
    4. briefing_enabled for AI briefing integration
    """

    @property
    def indexing_enabled(self) -> bool:
        return os.getenv("RETRIEVAL_INDEXING_ENABLED", "false").lower() == "true"

    @property
    def associative_enabled(self) -> bool:
        return os.getenv("RETRIEVAL_ASSOCIATIVE_ENABLED", "false").lower() == "true"

    @property
    def associative_enabled_entity_summary(self) -> bool:
        return os.getenv("RETRIEVAL_ASSOCIATIVE_ENTITY_SUMMARY", "false").lower() == "true"

    @property
    def associative_enabled_recent_memories(self) -> bool:
        return os.getenv("RETRIEVAL_ASSOCIATIVE_RECENT_MEMORIES", "false").lower() == "true"

    @property
    def associative_enabled_hindsight(self) -> bool:
        return os.getenv("RETRIEVAL_ASSOCIATIVE_HINDSIGHT", "false").lower() == "true"

    @property
    def associative_enabled_hydrate(self) -> bool:
        return os.getenv("RETRIEVAL_ASSOCIATIVE_HYDRATE", "false").lower() == "true"

    @property
    def shadow_mode(self) -> bool:
        return os.getenv("RETRIEVAL_SHADOW_MODE", "false").lower() == "true"

    @property
    def briefing_enabled(self) -> bool:
        return os.getenv("RETRIEVAL_BRIEFING_ENABLED", "false").lower() == "true"

    @property
    def debug_explanations(self) -> bool:
        return os.getenv("RETRIEVAL_DEBUG", "false").lower() == "true"

    @property
    def context_neighbors(self) -> bool:
        return os.getenv("RETRIEVAL_CONTEXT_NEIGHBORS", "false").lower() == "true"

    @property
    def chunk_enrichment(self) -> bool:
        return os.getenv("RETRIEVAL_CHUNK_ENRICHMENT", "false").lower() == "true"


config = RetrievalConfig()

# --- Constants ---
PASSAGE_MAX_CHARS = 1024
PASSAGE_OVERLAP_CHARS = 64
PASSAGE_MIN_CHARS = 80

PPR_DAMPING = 0.85
PPR_ITERATIONS = 20
PPR_TOLERANCE = 1e-6

TRIPLE_EXTRACTION_MODEL = CLASSIFICATION_MODEL

DEFAULT_TOP_K_PHRASES = 30
DEFAULT_TOP_K_PASSAGES = 10
DEFAULT_TOP_K_MEMORIES = 8

RECOGNITION_THRESHOLD = 0.55

BACKFILL_BATCH_SIZE = 20
BACKFILL_MAX_CONCURRENCY = 3

INDEX_VERSION = 1
