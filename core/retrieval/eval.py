import time
import json
from typing import List, Optional, Dict, Set
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.retrieval.schema import ExplainableBundle
from core.retrieval.search import associative_retrieve
from core.retrieval.config import config

supabase = get_supabase()

# ---------------------------------------------------------------------------
# Default questions (used when no ground-truth labels exist)
# ---------------------------------------------------------------------------
EVAL_QUESTIONS = [
    "What prior situation is similar to this?",
    "What should I remember before meeting Ashraya?",
    "Which people are connected to QHORD?",
    "What pattern is repeating in my week?",
    "What earlier decision is relevant for Solvstrat?",
    "What do I know about the church operations issue?",
    "Who have I discussed the Crayon project with?",
    "What deadlines are approaching this week?",
]

DEFAULT_TOP_K = 8

# Recall cutoffs to evaluate
K_VALUES = [5, 8, 12]


# ---------------------------------------------------------------------------
# Metric computation (pure functions, no DB dependency)
# ---------------------------------------------------------------------------
def compute_recall(expected: Set[int], returned: List[int], k: int) -> float:
    """Recall@k = |intersection(expected, top_k)| / |expected|.

    Returns 0.0 if expected is empty.
    """
    if not expected:
        return 0.0
    top_k = set(returned[:k])
    return len(expected & top_k) / len(expected)


def compute_precision(expected: Set[int], returned: List[int], k: int) -> float:
    """Precision@k = |intersection(expected, top_k)| / k.

    Returns 0.0 if k is 0.
    """
    if k == 0:
        return 0.0
    top_k = set(returned[:k])
    return len(expected & top_k) / k


def compute_metrics(
    expected_ids: Set[int], returned_ids: List[int], k_values: List[int] = K_VALUES
) -> Dict[str, Optional[float]]:
    """Compute recall and precision at each k value."""
    metrics: Dict[str, Optional[float]] = {}
    for k in k_values:
        metrics[f"recall_at_{k}"] = compute_recall(expected_ids, returned_ids, k)
        metrics[f"precision_at_{k}"] = compute_precision(expected_ids, returned_ids, k)
    return metrics


# ---------------------------------------------------------------------------
# Ground-truth loader
# ---------------------------------------------------------------------------
def load_ground_truth() -> Dict[str, Set[int]]:
    """Load ground-truth labels from retrieval_eval_gold table.

    Returns {query_text: {memory_id, ...}}.
    """
    try:
        res = supabase.table("retrieval_eval_gold") \
            .select("query_text, expected_memory_ids") \
            .execute()
        if not res or not res.data:
            return {}
        gt: Dict[str, Set[int]] = {}
        for row in res.data:
            q = row["query_text"]
            ids = row.get("expected_memory_ids")
            if isinstance(ids, str):
                ids = json.loads(ids)
            if isinstance(ids, list):
                gt[q] = {int(i) for i in ids if i is not None}
        return gt
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"load_ground_truth failed: {e}")
        return {}


def seed_ground_truth(entries: List[dict]) -> int:
    """Seed ground-truth entries. Each entry: {query_text, expected_memory_ids, category?, notes?}.

    Uses upsert on query_text to avoid duplicates.
    Returns number of entries inserted/updated.
    """
    count = 0
    for entry in entries:
        query = entry.get("query_text", "").strip()
        if not query:
            continue
        ids = entry.get("expected_memory_ids", [])
        if isinstance(ids, set):
            ids = list(ids)
        try:
            supabase.table("retrieval_eval_gold").upsert({
                "query_text": query,
                "expected_memory_ids": json.dumps(ids),
                "category": entry.get("category", "general"),
                "notes": entry.get("notes"),
            }, on_conflict="query_text").execute()
            count += 1
        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"seed_ground_truth failed for '{query[:40]}': {e}")
    return count


# ---------------------------------------------------------------------------
# Retrieval runners
# ---------------------------------------------------------------------------
async def _current_retrieval(query: str, top_k: int) -> list:
    """Run current Rhodey retrieval for comparison."""
    from core.pulse.memory import retrieve_hindsight_memories
    try:
        result = await retrieve_hindsight_memories(
            task_inputs=[query],
            active_tasks=[],
            top_k=top_k,
        )
        if result and isinstance(result, tuple) and len(result) > 0:
            items = result[0]
            if isinstance(items, list):
                return [{"id": str(i), "score": 0.5} for i in items]
            if isinstance(items, str):
                return [{"id": "result", "text": items[:200]}]
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def _serialize_current(result: list) -> list:
    """Serialize current retrieval results for storage."""
    if not result:
        return []
    return result[:10]


def _serialize_associative(bundle: ExplainableBundle) -> list:
    """Serialize associative retrieval results for storage."""
    return [
        {
            "memory_id": item.memory_id,
            "score": item.score,
            "explanation": item.explanation,
        }
        for item in bundle.items[:10]
    ]


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------
async def run_eval(
    run_name: str = "shadow_eval",
    run_type: str = "shadow",
    questions: Optional[List[str]] = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Run evaluation with recall@k metrics.

    Loads ground-truth labels from retrieval_eval_gold if available.
    Falls back to EVAL_QUESTIONS when no ground truth is seeded.
    """
    if not config.indexing_enabled and not config.shadow_mode:
        return {"status": "skipped", "reason": "retrieval_not_enabled"}

    # Load ground truth
    ground_truth = load_ground_truth()

    # Determine queries to run
    if questions:
        qs = questions
    elif ground_truth:
        qs = list(ground_truth.keys())
    else:
        qs = EVAL_QUESTIONS

    # Create eval run
    run_res = supabase.table("retrieval_eval_runs") \
        .insert({
            "run_name": run_name,
            "run_type": run_type,
            "total_queries": len(qs),
        }) \
        .execute()

    if not run_res or not run_res.data:
        return {"status": "failed", "reason": "cannot_create_run"}

    run_id = run_res.data[0]["id"]
    completed = 0

    # Aggregate metrics
    agg_recall = {f"recall_at_{k}": [] for k in K_VALUES}
    agg_precision = {f"precision_at_{k}": [] for k in K_VALUES}
    all_latencies_assoc = []

    for query_text in qs:
        try:
            # Current retrieval
            current_start = time.time()
            current_result = await _current_retrieval(query_text, top_k)
            current_latency = int((time.time() - current_start) * 1000)

            # Associative retrieval
            assoc_start = time.time()
            assoc_result = await associative_retrieve(query=query_text, top_k=top_k)
            assoc_latency = int((time.time() - assoc_start) * 1000)

            # Extract memory IDs from associative results
            assoc_memory_ids = [item.memory_id for item in assoc_result.items]

            # Compute metrics if ground truth available
            expected_ids = ground_truth.get(query_text, set())
            metrics = {}
            if expected_ids:
                metrics = compute_metrics(expected_ids, assoc_memory_ids)
                for k in K_VALUES:
                    agg_recall[f"recall_at_{k}"].append(metrics[f"recall_at_{k}"])
                    agg_precision[f"precision_at_{k}"].append(metrics[f"precision_at_{k}"])
            all_latencies_assoc.append(assoc_latency)

            # Store per-query result
            supabase.table("retrieval_eval_results") \
                .insert({
                    "run_id": run_id,
                    "query_text": query_text,
                    "current_top_k": _serialize_current(current_result),
                    "associative_top_k": _serialize_associative(assoc_result),
                    "current_latency_ms": current_latency,
                    "associative_latency_ms": assoc_latency,
                    "recall_at_5": metrics.get("recall_at_5"),
                    "recall_at_8": metrics.get("recall_at_8"),
                    "recall_at_12": metrics.get("recall_at_12"),
                    "precision_at_5": metrics.get("precision_at_5"),
                    "precision_at_8": metrics.get("precision_at_8"),
                    "precision_at_12": metrics.get("precision_at_12"),
                    "expected_count": len(expected_ids) if expected_ids else None,
                }) \
                .execute()

            completed += 1

        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"Eval query failed '{query_text[:50]}': {e}")

    # Aggregate summary
    summary = _aggregate_summary(
        agg_recall, agg_precision, all_latencies_assoc, completed
    )

    # Update run record
    supabase.table("retrieval_eval_runs") \
        .update({
            "status": "completed",
            "completed_queries": completed,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }) \
        .eq("id", run_id) \
        .execute()

    result = {
        "status": "completed",
        "run_id": run_id,
        "run_name": run_name,
        "total_queries": len(qs),
        "completed": completed,
        **summary,
    }

    # Print summary to stdout for CI visibility
    _print_summary(result)

    return result


def _aggregate_summary(
    agg_recall: Dict[str, List[float]],
    agg_precision: Dict[str, List[float]],
    latencies: List[int],
    completed: int,
) -> dict:
    """Compute aggregate metrics from per-query lists."""
    summary: dict = {}

    for k in K_VALUES:
        rkey = f"recall_at_{k}"
        pkey = f"precision_at_{k}"
        rvals = agg_recall.get(rkey, [])
        pvals = agg_precision.get(pkey, [])

        summary[f"mean_{rkey}"] = _mean(rvals) if rvals else None
        summary[f"mean_{pkey}"] = _mean(pvals) if pvals else None

    summary["mean_latency_ms"] = _mean(latencies) if latencies else None
    summary["p50_latency_ms"] = _percentile(latencies, 50) if latencies else None
    summary["p95_latency_ms"] = _percentile(latencies, 95) if latencies else None

    return summary


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: List[int], p: int) -> int:
    if not values:
        return 0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * p / 100)
    idx = min(idx, len(sorted_v) - 1)
    return sorted_v[idx]


def _print_summary(result: dict) -> None:
    """Print eval summary to stdout."""
    print("\n" + "=" * 60)
    print(f"  RETRIEVAL EVAL: {result.get('run_name', 'N/A')}")
    print("=" * 60)
    print(f"  Queries: {result.get('completed', 0)}/{result.get('total_queries', 0)}")

    for k in K_VALUES:
        rkey = f"mean_recall_at_{k}"
        pkey = f"mean_precision_at_{k}"
        r = result.get(rkey)
        p = result.get(pkey)
        if r is not None:
            print(f"  Recall@{k}:    {r:.3f}")
        if p is not None:
            print(f"  Precision@{k}: {p:.3f}")

    lat = result.get("mean_latency_ms")
    p50 = result.get("p50_latency_ms")
    p95 = result.get("p95_latency_ms")
    if lat is not None:
        print(f"  Latency:    mean={lat}ms  p50={p50}ms  p95={p95}ms")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Single-query comparison (used in shadow mode)
# ---------------------------------------------------------------------------
async def compare_retrievals(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    ground_truth: Optional[Dict[str, Set[int]]] = None,
) -> dict:
    """Side-by-side comparison of current vs associative retrieval for a single query.

    Includes recall@k metrics if ground truth exists for this query.
    Accepts optional ground_truth cache to avoid repeated DB hits.
    """
    current_start = time.time()
    current_result = await _current_retrieval(query, top_k)
    current_latency = int((time.time() - current_start) * 1000)

    assoc_start = time.time()
    assoc_result = await associative_retrieve(query=query, top_k=top_k)
    assoc_latency = int((time.time() - assoc_start) * 1000)

    assoc_memory_ids = [item.memory_id for item in assoc_result.items]

    # Use cached GT if provided, otherwise load from DB
    if ground_truth is None:
        ground_truth = load_ground_truth()
    expected_ids = ground_truth.get(query, set())
    metrics = {}
    if expected_ids:
        metrics = compute_metrics(expected_ids, assoc_memory_ids)

    return {
        "query": query,
        "current": {
            "count": len(current_result) if isinstance(current_result, list) else 1,
            "latency_ms": current_latency,
        },
        "associative": {
            "count": len(assoc_result.items),
            "latency_ms": assoc_latency,
            "top_items": [
                {"memory_id": i.memory_id, "score": i.score, "explanation": i.explanation}
                for i in assoc_result.items[:5]
            ],
        },
        **metrics,
    }
