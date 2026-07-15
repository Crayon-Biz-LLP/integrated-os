"""Unified Ingestion Pipeline — single entry point for all channel intake.

All channels (telegram, whatsapp, email, call, teams, multimodal) use
this function as their single contract for persisting classified messages
into the system.

Usage:
    result = await ingest(
        text="Message content",
        source="whatsapp",
        classification="actionable",  # or "fyi", "ignored"
        summary="Who sent it, what they want",
        suggested_title="Verb-first action" or None,
        suggested_project="SOLVSTRAT" or None,
        channel_specific_data={"sender_phone": "..."}
    )

The caller is responsible for:
  1. Fetching the message (via API, webhook, file poll)
  2. Classifying the message (using classify_intent or channel-specific prompt)
  3. Calling ingest() to persist

This replaces the per-channel duplicate classify/persist logic.
"""

from datetime import datetime, timezone
from typing import Optional

from core.lib.audit_logger import audit_log_sync
from core.lib.url_filter import check_and_quarantine_url
from core.services.db import get_supabase

supabase = get_supabase()


async def save_resource(text: str) -> bool:
    """Extract and save a URL from text as a resource.

    Uses the canonical URL quarantine filter.
    """
    result = check_and_quarantine_url(text, source="ingest")
    return result.is_url


async def ingest(
    text: str,
    source: str,
    classification: str = "note",
    summary: str = "",
    suggested_title: Optional[str] = None,
    suggested_project: Optional[str] = None,
    linked_person_id: Optional[int] = None,
    linked_project_id: Optional[int] = None,
    is_human_sender: bool = False,
    has_memory_value: bool = False,
    needs_draft: bool = False,
    channel_specific_data: Optional[dict] = None,
    tracking_id: Optional[str] = None,
    received_at: Optional[str] = None,
    body: Optional[str] = None,
) -> dict:
    """Persist a classified message into the system.

    Args:
        text: Raw message text (or URL)
        source: Channel name ("telegram", "whatsapp", "email", "call", "teams", "multimodal")
        classification: "actionable" | "fyi" | "ignored" | "note" | "resource"
        summary: 1-2 sentence summary
        suggested_title: Task title (for actionable items)
        suggested_project: Project name (e.g. "SOLVSTRAT")
        linked_person_id: Resolved person ID from people table
        linked_project_id: Resolved project ID from projects table
        is_human_sender: Whether the message is from a real person
        has_memory_value: Whether to create a memory/relationship note
        needs_draft: Whether an email draft should be generated
        channel_specific_data: Extra metadata to store
        tracking_id: Channel-specific dedup ID (e.g. Gmail message_id)
        received_at: ISO timestamp of when the message was received
        body: Full body text (for emails with truncated summary)

    Returns:
        dict with keys: status, action, message_id, memory_id, etc.
    """
    now = datetime.now(timezone.utc).isoformat()
    recv_at = received_at or now
    body_text = body or text

    # ── Route by classification ──
    if classification == "ignored":
        supabase.table('messages').insert({
            "channel": source,
            "source": source,
            "body": body_text[:20000],
            "summary": summary[:1000],
            "classification": "ignored",
            "processing_status": "completed",
            "danny_decision": "skipped",
            "received_at": recv_at,
            "metadata": channel_specific_data or {},
        }).execute()
        return {"status": "ignored"}

    if classification == "resource":
        await save_resource(text)
        return {"status": "filed", "action": "resource"}

    if classification == "note":
        row = {
            "channel": source,
            "source": source,
            "body": body_text[:20000],
            "summary": summary[:1000],
            "classification": "note",
            "has_memory_value": is_human_sender and has_memory_value,
            "processing_status": "completed",
            "received_at": recv_at,
            "metadata": channel_specific_data or {},
        }
        if tracking_id:
            row["message_id"] = tracking_id
        insert_res = supabase.table('messages').insert(row).execute()
        if not insert_res.data:
            return {"status": "error", "reason": "insert returned no data"}
        message_id = insert_res.data[0]['id']

        if has_memory_value and summary:
            from core.llm import get_embedding
            from core.retrieval.pipeline import schedule_index_memory
            from core.pulse.entity_extractor import extract_and_link_entities
            from core.lib.time_utils import compute_expires_at
            mem_content = f"{source}: {summary or text[:200]}"
            emb = (await get_embedding(mem_content)).vector
            mem_res = supabase.table('memories').insert({
                "content": mem_content,
                "memory_type": "relationship_note",
                "embedding": emb,
                "embedding_status": "success" if emb and any(emb) else "failed",
                "source": source,
                "expires_at": compute_expires_at(mem_content, recv_at),
            }).execute()
            if mem_res.data:
                memory_id = mem_res.data[0]['id']
                schedule_index_memory(memory_id, mem_content, "relationship_note", source)
                extract_and_link_entities(mem_content, str(memory_id), 'memory')

        return {"status": "filed", "action": "note", "message_id": message_id}

    # ── FYI or actionable: persist to messages table ──
    row = {
        "channel": source,
        "source": source,
        "body": body_text[:20000],
        "summary": summary[:1000],
        "classification": classification,
        "suggested_title": suggested_title,
        "suggested_project": suggested_project,
        "linked_person_id": linked_person_id,
        "linked_project_id": linked_project_id,
        "is_human_sender": is_human_sender,
        "has_memory_value": has_memory_value,
        "processing_status": "completed",
        "received_at": recv_at,
        "metadata": channel_specific_data or {},
    }

    if tracking_id:
        row["message_id"] = tracking_id

    # Check channel_specific_data for danny_decision override (e.g. dedup_decision from email ingest)
    csd = channel_specific_data or {}
    explicit_dd = csd.get("danny_decision")  # 'skipped', 'merged', or None

    if classification == "fyi":
        row["danny_decision"] = explicit_dd or None
    elif classification == "actionable":
        if explicit_dd in ("skipped", "merged"):
            row["danny_decision"] = explicit_dd
            # If the decision was to skip, mark classification as ignored for the insert
            # so it doesn't appear in Decision Pulse
            row["classification"] = "ignored" if explicit_dd == "skipped" else classification

    insert_res = supabase.table('messages').insert(row).execute()
    if not insert_res.data:
        return {"status": "error", "reason": "insert returned no data"}

    message_id = insert_res.data[0]['id']

    # ── Save relationship note if human-sent with memory value ──
    if is_human_sender and has_memory_value and summary:
        from core.llm import get_embedding
        from core.retrieval.pipeline import schedule_index_memory
        from core.pulse.entity_extractor import extract_and_link_entities
        from core.lib.time_utils import compute_expires_at

        mem_content = f"{source}: {summary}"
        emb = (await get_embedding(mem_content)).vector
        mem_res = supabase.table('memories').insert({
            "content": mem_content,
            "memory_type": "relationship_note",
            "embedding": emb,
            "embedding_status": "success" if emb and any(emb) else "failed",
            "source": source,
            "expires_at": compute_expires_at(mem_content, recv_at),
        }).execute()
        if mem_res.data:
            memory_id = mem_res.data[0]['id']
            schedule_index_memory(memory_id, mem_content, "relationship_note", source)
            extract_and_link_entities(mem_content, str(memory_id), 'memory')

    # ── Generate draft if needed ──
    if needs_draft and classification == "actionable":
        try:
            from core.skills.email_ingest import generate_draft
            sender = (channel_specific_data or {}).get("sender_name") or source
            draft_body = await generate_draft(sender, suggested_title or "", body_text)
            if draft_body:
                supabase.table('email_drafts').insert({
                    "message_id": message_id,
                    "draft_body": draft_body,
                    "status": "pending",
                }).execute()
        except Exception as e:
            audit_log_sync("ingest", "WARNING", f"Draft generation failed: {e}")

    return {"status": "filed", "classification": classification, "message_id": message_id}
