from typing import Dict, List, Set, Tuple
from core.retrieval.config import PPR_DAMPING, PPR_ITERATIONS, PPR_TOLERANCE


def personalized_pagerank(
    adjacency: Dict[int, List[Tuple[int, float]]],
    seed_nodes: Dict[int, float],
    damping: float = PPR_DAMPING,
    iterations: int = PPR_ITERATIONS,
    tolerance: float = PPR_TOLERANCE,
) -> Dict[int, float]:
    """Run Personalized PageRank on a graph.

    Args:
        adjacency: {node_id: [(neighbor_id, weight), ...]}
        seed_nodes: {node_id: initial_score} — query-seeded nodes
        damping: teleport probability (1-damping = restart prob)
        iterations: max iterations
        tolerance: convergence threshold

    Returns:
        {node_id: ppr_score} for all reachable nodes
    """
    if not adjacency or not seed_nodes:
        return {}

    all_nodes: Set[int] = set(adjacency.keys())
    for neighbors in adjacency.values():
        for nid, _ in neighbors:
            all_nodes.add(nid)
    for sid in seed_nodes:
        all_nodes.add(sid)

    if not all_nodes:
        return {}

    seed_total = sum(seed_nodes.values())
    if seed_total == 0:
        return {}

    teleport = {n: seed_nodes.get(n, 0.0) / seed_total for n in all_nodes}

    scores = {n: teleport.get(n, 0.0) for n in all_nodes}

    for _ in range(iterations):
        prev = scores.copy()
        max_delta = 0.0

        for node in all_nodes:
            incoming = 0.0
            for neighbor, weight in adjacency.get(node, []):
                out_degree = len(adjacency.get(neighbor, []))
                if out_degree > 0:
                    incoming += prev[neighbor] * (weight / out_degree)

            scores[node] = (1.0 - damping) * teleport.get(node, 0.0) + damping * incoming

            delta = abs(scores[node] - prev[node])
            if delta > max_delta:
                max_delta = delta

        if max_delta < tolerance:
            break

    return scores


def build_adjacency_from_edges(
    edges: List[Tuple[int, int, float]],
) -> Dict[int, List[Tuple[int, float]]]:
    """Build an adjacency dict from a list of (from_node, to_node, weight) edges."""
    adj: Dict[int, List[Tuple[int, float]]] = {}
    for from_id, to_id, weight in edges:
        if from_id not in adj:
            adj[from_id] = []
        adj[from_id].append((to_id, weight))
    return adj


def normalize_scores(scores: Dict[int, float]) -> Dict[int, float]:
    """Min-max normalize scores to [0, 1] range."""
    if not scores:
        return {}
    vals = list(scores.values())
    mn, mx = min(vals), max(vals)
    if mx - mn < 1e-10:
        return {k: 1.0 for k in scores}
    return {k: (v - mn) / (mx - mn) for k, v in scores.items()}
