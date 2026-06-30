from enum import Enum
import uuid
import contextvars
from core.lib.audit_logger import audit_log_sync


class ReasonCode(str, Enum):
    NO_ENTITY_OVERLAP = "no_entity_overlap"
    BELOW_THRESHOLD = "below_threshold"
    FACT_SOURCE_PRIORITY = "fact_source_priority"
    CROSS_PROJECT_ADJACENCY = "cross_project_adjacency"
    TOP_K_TRUNCATED = "top_k_truncated"
    NEUTRAL_DOWNGRADED = "neutral_downgraded"
    HARD_GATE_REJECTED = "hard_gate_rejected"
    SOFT_GATE_DOWNGRADED = "soft_gate_downgraded"
    SEMANTIC_SKIPPED_NO_ANCHOR = "semantic_skipped_no_anchor"
    RETRIEVED = "retrieved"


class DecisionStage(str, Enum):
    CLASSIFICATION = "classification"
    ROUTING = "routing"
    CONTEXT_REGISTRY = "context_registry"
    RETRIEVAL = "retrieval"


decision_chain_id_var = contextvars.ContextVar('decision_chain_id', default=None)


def new_decision_chain_id() -> str:
    return str(uuid.uuid4())


def set_decision_chain_id(chain_id: str = None) -> str:
    if not chain_id:
        chain_id = new_decision_chain_id()
    decision_chain_id_var.set(chain_id)
    return chain_id


def get_decision_chain_id() -> str:
    cid = decision_chain_id_var.get()
    return cid or ""


async def log_decision(
    stage: DecisionStage,
    query_text: str = "",
    resolved_entities: list = None,
    included_items: list = None,
    excluded_items: list = None,
    reason_codes: list = None,
    summary: str = ""
):
    cid = get_decision_chain_id()
    if not cid:
        return

    audit_log_sync("decision_audit", "INFO", summary or f"Decision: {stage}", {
        "decision_chain_id": cid,
        "stage": stage,
        "query_text": query_text[:200] if query_text else "",
        "resolved_entities": resolved_entities or [],
        "included_items": _truncate_items(included_items) if included_items else [],
        "excluded_items": _truncate_items(excluded_items, with_reason=True) if excluded_items else [],
        "reason_codes": reason_codes or [],
        "summary": summary[:300] if summary else ""
    })


def _truncate_items(items: list, with_reason: bool = False, max_items: int = 5, content_max: int = 100) -> list:
    out = []
    for item in items[:max_items]:
        entry = {
            "id": item.get("id", ""),
            "content": item.get("content", "")[:content_max],
            "score": item.get("score", 0),
            "source": item.get("source", ""),
        }
        if with_reason and "reason" in item:
            entry["reason"] = item["reason"]
        out.append(entry)
    return out
