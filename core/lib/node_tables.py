"""
Abstraction layer for pending_nodes and merge_proposals tables.

- pending_nodes: Node creation approvals (person, org, project, etc.)
- merge_proposals: Merge target→source proposals
"""

from typing import Optional
from core.services.db import get_supabase, maybe_single_safe
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()

# ── pending_nodes ──────────────────────────────────────

def insert_pending_node(
    label: str,
    node_type: str,
    source_text: str = "",
    context: str = None,
    eval_context: dict = None,
    status: str = "pending",
) -> Optional[int]:
    """Insert a new pending node. Returns id or None on failure."""
    try:
        res = supabase.table("pending_nodes").insert({
            "label": label,
            "node_type": node_type,
            "source_text": source_text,
            "context": context,
            "eval_context": eval_context or {},
            "status": status,
        }).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"insert_pending_node failed for {label}: {e}")
    return None


def get_pending_node(node_id: int) -> Optional[dict]:
    """Fetch a pending node by id."""
    try:
        res = maybe_single_safe(
            supabase.table("pending_nodes").select("*").eq("id", node_id))
        if res and res.data:
            return res.data
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"get_pending_node({node_id}) failed: {e}")
    return None


def update_pending_node_status(
    node_id: int, status: str, resolved_at: bool = True
) -> bool:
    """Update pending node status. Optionally set resolved_at."""
    try:
        update = {"status": status}
        if resolved_at:
            update["resolved_at"] = "now()"
        supabase.table("pending_nodes").update(update).eq("id", node_id).execute()
        return True
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"update_pending_node_status({node_id}) failed: {e}")
        return False


def list_pending_nodes(
    statuses: list = None,
    node_types: list = None,
    limit: int = 50,
) -> list:
    """List pending nodes filtered by status and/or type."""
    try:
        query = supabase.table("pending_nodes").select(
            "id, label, node_type, source_text, status, created_at, eval_context")
        if statuses:
            query = query.in_("status", statuses)
        if node_types:
            query = query.in_("node_type", node_types)
        res = query.order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"list_pending_nodes failed: {e}")
        return []


# ── merge_proposals ────────────────────────────────────

def insert_merge_proposal(
    source_label: str,
    source_type: str,
    target_node_id: str,
    target_label: str,
    source_node_id: str = None,
    rationale: str = "",
) -> Optional[int]:
    """Insert a new merge proposal. Returns id or None."""
    try:
        # Check if already proposed
        existing = maybe_single_safe(
            supabase.table("merge_proposals")
            .select("id, status")
            .eq("source_label", source_label)
            .eq("target_node_id", target_node_id)
            .neq("status", "rejected")
        )
        if existing and existing.data:
            if existing.data["status"] == "proposed":
                return None  # Already proposed
            # Re-propose
            supabase.table("merge_proposals").update({
                "status": "proposed",
                "rationale": rationale,
            }).eq("id", existing.data["id"]).execute()
            return existing.data["id"]

        res = supabase.table("merge_proposals").insert({
            "source_label": source_label,
            "source_type": source_type,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "target_label": target_label,
            "rationale": rationale,
        }).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"insert_merge_proposal failed: {e}")
    return None


def resolve_merge_proposal(
    proposal_id: int,
    status: str,  # 'accepted' or 'rejected'
) -> bool:
    """Resolve a merge proposal."""
    try:
        supabase.table("merge_proposals").update({
            "status": status,
            "resolved_at": "now()",
        }).eq("id", proposal_id).execute()
        return True
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"resolve_merge_proposal({proposal_id}) failed: {e}")
        return False


def list_pending_merge_proposals(limit: int = 20) -> list:
    """List open merge proposals."""
    try:
        res = supabase.table("merge_proposals").select(
            "id, source_label, source_type, target_label, rationale, proposed_at"
        ).eq("status", "proposed").order("proposed_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        audit_log_sync("node_tables", "WARNING",
                       f"list_merge_proposals failed: {e}")
        return []


