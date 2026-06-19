from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class WeightConfig:
    semantic: float = 0.25
    ppr: float = 0.25
    specificity: float = 0.10
    recency: float = 0.15
    importance: float = 0.10
    project_boost: float = 0.10
    person_boost: float = 0.05


DEFAULT_WEIGHTS = WeightConfig()


def rank_memories(
    memory_scores: Dict[int, float],
    ppr_scores: Optional[Dict[int, float]] = None,
    semantic_scores: Optional[Dict[int, float]] = None,
    specificity_boost: Optional[Dict[int, float]] = None,
    recency_boost: Optional[Dict[int, float]] = None,
    importance_boost: Optional[Dict[int, float]] = None,
    project_boost: Optional[Dict[int, float]] = None,
    person_boost: Optional[Dict[int, float]] = None,
    weights: WeightConfig = DEFAULT_WEIGHTS,
) -> List[tuple]:
    """Blend multiple score signals into a final ranking.
    
    Args:
        memory_scores: base scores per memory_id
        ppr_scores: PPR-derived scores per memory_id (normalized 0-1)
        semantic_scores: semantic similarity scores per memory_id (normalized 0-1)
        specificity_boost: per-memory specificity boost (0-1)
        recency_boost: per-memory recency boost (0-1)
        importance_boost: per-memory importance boost (0-1)
        project_boost: per-memory active project boost (0-1)
        person_boost: per-memory active person boost (0-1)
        weights: weight configuration

    Returns:
        List of (memory_id, blended_score) sorted descending
    """
    if not memory_scores:
        return []

    all_ids = set(memory_scores.keys())

    def _safe(d, key, default=0.0):
        return d.get(key, default) if d else default

    def pp_norm(d):
        if not d:
            return {}
        vals = list(d.values())
        mx = max(vals) if vals else 1.0
        mn = min(vals) if vals else 0.0
        rng = mx - mn
        if rng < 1e-10:
            return {k: 0.5 for k in d}
        return {k: (v - mn) / rng for k, v in d.items()}

    ppr_norm = pp_norm(ppr_scores) if ppr_scores else {}
    sem_norm = pp_norm(semantic_scores) if semantic_scores else {}
    rec_norm = pp_norm(recency_boost) if recency_boost else {}
    imp_norm = pp_norm(importance_boost) if importance_boost else {}
    proj_norm = pp_norm(project_boost) if project_boost else {}
    pers_norm = pp_norm(person_boost) if person_boost else {}
    spec_norm = pp_norm(specificity_boost) if specificity_boost else {}

    ranked = []
    for mid in all_ids:
        score = (
            weights.ppr * _safe(ppr_norm, mid) +
            weights.semantic * _safe(sem_norm, mid) +
            weights.specificity * _safe(spec_norm, mid) +
            weights.recency * _safe(rec_norm, mid) +
            weights.importance * _safe(imp_norm, mid) +
            weights.project_boost * _safe(proj_norm, mid) +
            weights.person_boost * _safe(pers_norm, mid)
        )
        ranked.append((mid, score))

    ranked.sort(key=lambda x: -x[1])
    return ranked
