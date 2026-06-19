import time
from typing import List, Optional
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync
from core.retrieval.schema import ExplainableBundle
from core.retrieval.search import associative_retrieve
from core.retrieval.config import config

supabase = get_supabase()

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


async def run_eval(
    run_name: str = "shadow_eval",
    run_type: str = "shadow",
    questions: Optional[List[str]] = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    if not config.indexing_enabled and not config.shadow_mode:
        return {"status": "skipped", "reason": "retrieval_not_enabled"}


    qs = questions or EVAL_QUESTIONS

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

    for query_text in qs:
        try:
            current_start = time.time()
            current_result = await _current_retrieval(query_text, top_k)
            current_latency = int((time.time() - current_start) * 1000)

            assoc_start = time.time()
            assoc_result = await associative_retrieve(query=query_text, top_k=top_k)
            assoc_latency = int((time.time() - assoc_start) * 1000)

            supabase.table("retrieval_eval_results") \
                .insert({
                    "run_id": run_id,
                    "query_text": query_text,
                    "current_top_k": _serialize_current(current_result),
                    "associative_top_k": _serialize_associative(assoc_result),
                    "current_latency_ms": current_latency,
                    "associative_latency_ms": assoc_latency,
                }) \
                .execute()

            completed += 1

        except Exception as e:
            audit_log_sync("retrieval", "WARNING",
                           f"Eval query failed '{query_text[:50]}': {e}")

    supabase.table("retrieval_eval_runs") \
        .update({
            "status": "completed",
            "completed_queries": completed,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }) \
        .eq("id", run_id) \
        .execute()

    return {
        "status": "completed",
        "run_id": run_id,
        "total_queries": len(qs),
        "completed": completed,
    }


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


async def compare_retrievals(
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Side-by-side comparison of current vs new retrieval for a single query.
    
    Used in shadow mode during normal operation.
    """
    current_start = time.time()
    current_result = await _current_retrieval(query, top_k)
    current_latency = int((time.time() - current_start) * 1000)

    assoc_start = time.time()
    assoc_result = await associative_retrieve(query=query, top_k=top_k)
    assoc_latency = int((time.time() - assoc_start) * 1000)

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
    }
