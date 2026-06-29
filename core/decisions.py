"""Structured Decision Log — CRUD operations for the decisions table.

Replaces implicit decision tracking (audit_logs + clarification_feedback)
with a first-class decision model that supports:
- Decision type, context, rationale
- Status lifecycle (active → superseded / reversed / expired)
- Entity linking (task, project, organization, person)
- Confidence tracking and source attribution
"""

from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


def record_decision(
    decision_type: str,
    title: str,
    context: str = None,
    rationale: str = None,
    entity_type: str = None,
    entity_id: str = None,
    organization_id: int = None,
    project_id: int = None,
    confidence: float = 1.0,
    source: str = "manual",
    source_ref: str = None,
    expires_at: str = None,
) -> dict:
    """Record a new decision. Returns the inserted row."""
    supabase = get_supabase()
    data = {
        "decision_type": decision_type,
        "title": title,
        "status": "active",
        "confidence": confidence,
        "source": source,
    }
    if context:
        data["context"] = context
    if rationale:
        data["rationale"] = rationale
    if entity_type:
        data["entity_type"] = entity_type
    if entity_id:
        data["entity_id"] = entity_id
    if organization_id:
        data["organization_id"] = organization_id
    if project_id:
        data["project_id"] = project_id
    if source_ref:
        data["source_ref"] = source_ref
    if expires_at:
        data["expires_at"] = expires_at
    else:
        # Default expiry: 30 days for approvals, 7 days for rejections, 90 days for completions
        if 'rejection' in decision_type:
            data["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        elif 'completion' in decision_type:
            data["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        else:
            data["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    try:
        res = supabase.table("decisions").insert(data).execute()
        if res.data:
            audit_log_sync("decisions", "INFO",
                           f"Recorded decision: [{decision_type}] {title} (confidence={confidence})")
            return res.data[0]
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to record decision: {e}")
    return {}


def supersede_decision(decision_id: int, title: str = None, rationale: str = None) -> bool:
    """Mark an active decision as superseded. Optionally record the new decision."""
    supabase = get_supabase()
    try:
        supabase.table("decisions").update({
            "status": "superseded"
        }).eq("id", decision_id).eq("status", "active").execute()

        audit_log_sync("decisions", "INFO", f"Superseded decision #{decision_id}")

        if title:
            new = record_decision(
                decision_type="supersession",
                title=title,
                rationale=rationale,
                source="manual",
            )
            if new.get("id"):
                supabase.table("decisions").update({
                    "superseded_by": new["id"]
                }).eq("id", decision_id).execute()

        return True
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to supersede decision #{decision_id}: {e}")
        return False


def reverse_decision(decision_id: int, rationale: str = None) -> bool:
    """Mark a decision as reversed (the decision was wrong)."""
    supabase = get_supabase()
    try:
        update_data = {"status": "reversed"}
        supabase.table("decisions").update(update_data).eq("id", decision_id).execute()
        audit_log_sync("decisions", "INFO",
                       f"Reversed decision #{decision_id}" + (f": {rationale}" if rationale else ""))
        return True
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to reverse decision #{decision_id}: {e}")
        return False


def get_active_decisions(
    decision_type: str = None,
    entity_type: str = None,
    entity_id: str = None,
    project_id: int = None,
    limit: int = 20,
) -> list:
    """Fetch active decisions, optionally filtered."""
    supabase = get_supabase()
    try:
        query = supabase.table("decisions").select("*").eq("status", "active")
        if decision_type:
            query = query.eq("decision_type", decision_type)
        if entity_type:
            query = query.eq("entity_type", entity_type)
        if entity_id:
            query = query.eq("entity_id", entity_id)
        if project_id:
            query = query.eq("project_id", project_id)
        res = query.order("decided_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to fetch active decisions: {e}")
        return []


def get_recent_decisions(limit: int = 10) -> list:
    """Fetch the most recent decisions (any status)."""
    supabase = get_supabase()
    try:
        res = supabase.table("decisions").select("*").order(
            "decided_at", desc=True
        ).limit(limit).execute()
        return res.data or []
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to fetch recent decisions: {e}")
        return []


def expire_stale_decisions() -> int:
    """Mark expired decisions (past their expires_at). Returns count expired."""
    supabase = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        res = supabase.table("decisions").update({"status": "expired"}).eq(
            "status", "active"
        ).not_.is_("expires_at", "null").lt("expires_at", now_iso).execute()
        count = len(res.data or [])
        if count:
            audit_log_sync("decisions", "INFO", f"Expired {count} stale decisions")
        return count
    except Exception as e:
        audit_log_sync("decisions", "ERROR", f"Failed to expire stale decisions: {e}")
        return 0
