"""WhatsApp ingest — hardened classification pipeline.

Pipeline (Phase 1 of the thread-aware classification design):
  Stage 0  split_chat_identity   → chat_id + participant (exact chat keys)
  Stage A  classify_sieve        → free deterministic noise drop (media/emoji/
                                   reactions/automated senders & notifications)
  Stage B  should_escalate       → free cost filter: only ask-like messages
                                   reach the LLM
  Stage C  classify_whatsapp_message → LLM with EPISODE CONTEXT (same chat,
                                   30-min silence boundary, ≤12 msgs) + graph
                                   knowledge + salience signals

All stages fail open (never 500 the ingest); the LLM is only called for the
ask fraction. Verified against the golden set (tests/golden/whatsapp_classify).
"""

from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import json
import asyncio
from datetime import datetime, timezone, timedelta
from core.retrieval.pipeline import schedule_index_memory
from core.lib.entity_context import extract_context_from_source
from core.services.db import tenant_aware_client
from core.services.llm import call_gemini_classify
from core.lib.time_utils import resolve_expiry
from core.lib.chat_split import split_chat_identity, normalize_chat_key
from core.lib.message_sieve import classify_sieve
from core.lib.ask_detector import should_escalate
from core.lib.episode_context import (
    fetch_episode,
    format_episode_lines,
    resolve_graph_knowledge,
)

supabase = tenant_aware_client()


async def classify_whatsapp_message(
    sender_name: str,
    sender_phone: str,
    message_text: str,
    chat_id: str = "",
    episode_lines: list[str] | None = None,
    graph_knowledge: str = "",
    user_name: str | None = None,
) -> dict:
    """LLM classification WITH thread + graph context (Stage C).

    Args:
        sender_name: sender display name (may be "Chat: Participant")
        sender_phone: sender id / chat stamp
        message_text: the message body
        chat_id: Stage-0 chat key (group prefix or 1:1 name)
        episode_lines: rendered episode window lines (oldest first)
        graph_knowledge: compact resolved-knowledge lines (or "")
        user_name: the user's name (mention detection)
    """
    from core.services.user_settings import resolve_user_name
    _user_name = user_name or resolve_user_name()

    episode_section = ""
    if episode_lines:
        episode_section = (
            "EPISODE CONTEXT (recent messages in this chat, oldest first — "
            "use these only as context for the NEW message):\n"
            + "\n".join(episode_lines)
            + "\n"
        )

    graph_section = ""
    if graph_knowledge:
        graph_section = f"KNOWLEDGE GRAPH (verified context — if a name is not here it is unknown, do not guess):\n{graph_knowledge}\n"

    mention_hint = ""
    if user_name:
        mention_hint = (
            f"\nIf the message mentions {_user_name} (or asks about them), set "
            f'"mentions_user": true.'
        )

    prompt = f"""You are classifying a WhatsApp message for {_user_name}.

MAILBOX CONTEXT: This is {_user_name}'s PERSONAL WhatsApp. It receives messages from family, friends, church contacts, and personal relationships. Work-related messages (clients, vendors, team) should be treated as actionable.

Sender: {sender_name or sender_phone}
Chat: {chat_id or (sender_name or sender_phone)}{episode_section}{graph_section}NEW MESSAGE: {message_text[:1000]}

CLASSIFICATION RULES

CLASSIFY AS "ignored" IF ANY:
- Automated or service message (OTP, notification, delivery update, payment alert)
- Group broadcast or mass-forwarded message with no personal context
- Promotional or spam message from an unknown number
- Trivial chit-chat, reactions, or casual filler that is NOT worth {_user_name} seeing

CLASSIFY AS "fyi" ONLY IF:
- A real person sharing information, updates, or context that is genuinely worth {_user_name} seeing — but no response or action is needed
- "fyi" means WORTH SEEING. Do NOT mark ordinary chit-chat as fyi — that is "ignored".

CLASSIFY AS "actionable" IF:
- A real person asking {_user_name} to do something, respond, decide, coordinate, or take action
- A request related to family, church, work, or personal obligations
- When in doubt, surface it as actionable

OUTPUT RULES

suggested_title:
- Verb-first, specific action (e.g., "Call Amma about Sunday lunch", "Review the pricing page", "Confirm prayer meeting time with Elder Thomas")
- NULL if fyi or ignored
- NULL if action cannot be stated specifically

suggested_project:
- One of the user's routing domains, or INBOX
- NULL if unsure

linked_person_name:
- Full name of the person mentioned or sending the message if identifiable
- NULL if unknown

has_memory_value:
- true if the message contains a decision, commitment, relationship context, or information worth remembering weeks later
- false for routine or trivial chat

mentions_user:
- true if the message mentions {_user_name} by name or asks something of them
- false otherwise

urgency:
- "urgent" only if the message signals urgency (urgent, asap, today, deadline)
- otherwise "normal"; "routine" for pure information

Return ONLY valid JSON, NO markdown, NO explanation:
{{
  "classification": "ignored|fyi|actionable",
  "summary": "1-2 sentences. Who sent it, what they want or shared.",
  "suggested_title": "verb-first task or null",
  "suggested_project": "project tag or null",
  "linked_person_name": "name or null",
  "has_memory_value": true or false,
  "mentions_user": true or false,
  "urgency": "urgent|normal|routine"
}}{mention_hint}"""

    response = await call_gemini_classify(
        prompt,
        model=CLASSIFICATION_MODEL,
        config={"response_mime_type": "application/json"}
    )
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        print(f"WhatsApp classify JSON parse failed, text={response.text[:200]}")
        return {"classification": "fyi", "summary": "Unparseable message", "suggested_title": None, "suggested_project": None, "linked_person_name": None, "has_memory_value": False}


def _ignored_row(sender_name, sender_phone, chat_id, participant, message_text, now_iso, summary, expires_iso, event_id=None):
    metadata = {"sender_phone": sender_phone, "chat_id": chat_id}
    if participant:
        metadata["participant"] = participant
    row = {
        "channel": "whatsapp",
        "source": "whatsapp",
        "sender_name": sender_name or sender_phone,
        "sender_id": sender_phone,
        "body": message_text.strip(),
        "classification": "ignored",
        "summary": summary,
        "suggested_title": None,
        "suggested_project": None,
        "has_memory_value": False,
        "received_at": now_iso,
        "processing_status": "completed",
        "metadata": metadata,
        "danny_decision": "skipped",
        "expires_at": expires_iso,
    }
    # Native Matrix event id → exact dedup on ignored rows too (the
    # unique_channel_message constraint protects them from re-delivery).
    if event_id:
        row["message_id"] = event_id
    return row


async def process_whatsapp_message(
    sender_name: str,
    sender_phone: str,
    message_text: str,
    received_at: str = None,
    chat_id: str | None = None,
    participant: str | None = None,
    event_id: str | None = None,
) -> dict:
    """Classify + persist one WhatsApp message.

    chat_id/participant: optional explicit Stage-0 identity overrides —
    the Beeper bridge already resolved the room identity (room name or
    WhatsApp phone) and passes it here, because the Matrix stream has no
    "Chat: Participant" string to split. Defaults to the legacy
    split_chat_identity() derivation when omitted (MacroDroid path).

    event_id: native Matrix event id — passed through to the batch RPC so
    the DB dedups re-delivered events exactly (unique_channel_message).
    """
    print(f"Processing WhatsApp message from {sender_name or sender_phone}: {message_text[:60]}...")

    # ── Dedup (unchanged) ────────────────────────────────────────────
    existing = supabase.table('messages')\
        .select('id')\
        .eq('channel', 'whatsapp')\
        .eq('sender_id', sender_phone)\
        .eq('body', message_text.strip())\
        .gte('received_at', (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())\
        .maybe_single()\
        .execute()
    if existing is not None and existing.data:
        print(f"Duplicate WhatsApp message from {sender_phone}, skipping.")
        return {"status": "duplicate", "message": "Already processed"}

    now_iso = received_at or datetime.now(timezone.utc).isoformat()
    created_at_dt = datetime.fromisoformat(now_iso)
    if created_at_dt.tzinfo is None:
        created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
    expires_at = resolve_expiry(message_text, created_at_dt)
    expires_iso = expires_at.isoformat() if expires_at else None

    # ── Stage 0: exact chat identity ─────────────────────────────────
    # Explicit overrides win (Beeper bridge passes room-resolved identity);
    # otherwise derive from the sender string (legacy MacroDroid stamps).
    if chat_id is None:
        identity = split_chat_identity(sender_phone)
        chat_id = identity["chat_id"] or normalize_chat_key(sender_name)
        if participant is None:
            participant = identity["participant"]

    # ── Stage A: deterministic sieve (free) ──────────────────────────
    sieve = classify_sieve(message_text, sender_name=sender_name, participant=participant)
    if sieve["noise"]:
        row = _ignored_row(sender_name, sender_phone, chat_id, participant,
                           message_text, now_iso, f"Sieve: {sieve['reason']}", expires_iso,
                           event_id)
        supabase.table('messages').insert(row).execute()
        print(f"[sieve:{sieve['reason']}] {sender_name or sender_phone}: {message_text[:60]}")
        return {"status": "ignored", "classification": "ignored", "stage": "sieve"}

    # ── Stage B: ask-detector (free) ─────────────────────────────────
    from core.services.user_settings import resolve_user_name
    _user_name = resolve_user_name()
    ask = should_escalate(message_text, user_name=_user_name)
    if not ask["escalate"]:
        row = _ignored_row(sender_name, sender_phone, chat_id, participant,
                           message_text, now_iso, "No ask detected — not worth surfacing", expires_iso,
                           event_id)
        supabase.table('messages').insert(row).execute()
        print(f"[ask-detector:no-escalation] {sender_name or sender_phone}: {message_text[:60]}")
        return {"status": "ignored", "classification": "ignored", "stage": "ask_detector"}

    # ── Stage C: context-aware LLM classification ────────────────────
    try:
        episode = fetch_episode(supabase, chat_id, now_iso)
        episode_lines = format_episode_lines(episode)
        graph_knowledge = resolve_graph_knowledge(supabase, sender_name, chat_id, _user_name)
        classification_data = await classify_whatsapp_message(
            sender_name, sender_phone, message_text,
            chat_id=chat_id,
            episode_lines=episode_lines,
            graph_knowledge=graph_knowledge,
            user_name=_user_name,
        )
    except Exception as e:
        print(f"Classification failed for {sender_phone}: {e}")
        classification_data = {
            "classification": "ignored", "summary": "Classification error",
            "suggested_title": None, "suggested_project": None,
            "linked_person_name": None, "has_memory_value": False,
        }

    classification = classification_data.get('classification', 'ignored')

    if classification == 'ignored':
        row = _ignored_row(sender_name, sender_phone, chat_id, participant,
                           message_text, now_iso,
                           classification_data.get('summary', 'Ignored'), expires_iso,
                           event_id)
        supabase.table('messages').insert(row).execute()
        print(f"[ignored] {sender_name or sender_phone}: {message_text[:60]}")
        return {"status": "ignored", "classification": classification}

    # ── Actionable and FYI: atomically batch or insert via RPC ───────
    rpc_args = {
        'p_sender_id': sender_phone,
        'p_sender_name': sender_name or sender_phone,
        'p_body': message_text.strip(),
        'p_received_at': now_iso,
        'p_classification': classification,
        'p_summary': classification_data.get('summary', ''),
        'p_suggested_title': classification_data.get('suggested_title'),
        'p_suggested_project': classification_data.get('suggested_project'),
        'p_has_memory_value': classification_data.get('has_memory_value', False),
        'p_linked_person_name': classification_data.get('linked_person_name'),
        'p_expires_at': expires_iso,
        'p_chat_id': chat_id,
        'p_participant': participant,
        'p_message_id': event_id,
    }

    result = supabase.rpc('batch_whatsapp_message', rpc_args).execute()
    action = result.data.get('action')
    final_class = result.data.get('classification', classification)

    if action == 'batched':
        print(f"[{final_class}] {sender_phone}: Batched into row {result.data['message_id']}")
        return {"status": "batched", "classification": final_class}

    if final_class == 'fyi':
        if classification_data.get('has_memory_value'):
            mem_content = f"{sender_name or sender_phone}: {classification_data.get('summary', message_text[:200])}"
            embedding = (await get_embedding(mem_content)).vector
            mem_result = supabase.table('memories').insert({
                "content": mem_content,
                "memory_type": "relationship_note",
                "embedding": embedding,
                "embedding_status": 'success' if embedding and any(embedding) else 'failed',
                "source": "whatsapp",
                "expires_at": expires_iso
            }).execute()
            memory_id = mem_result.data[0]['id']
            schedule_index_memory(memory_id, mem_content, "relationship_note", "whatsapp")
            # WhatsApp produces noisy entity extraction — skip pending node
            # creation; entities detected for graph linking only.
            ctx = await extract_context_from_source(mem_content, timing="async", create_pending=False)
            if ctx.organization_id:
                supabase.table('memories').update({'organization_id': ctx.organization_id}).eq('id', memory_id).execute()
            elif ctx.pending_org_id:
                supabase.table('memories').update({'pending_org_id': ctx.pending_org_id}).eq('id', memory_id).execute()
        print(f"[fyi] {sender_name or sender_phone}: {message_text[:60]}")
        return {"status": "fyi", "classification": final_class}

    # actionable
    print(f"[actionable] {sender_name or sender_phone}: {classification_data.get('suggested_title', message_text[:60])}")
    return {
        "status": "actionable",
        "classification": final_class,
        "suggested_title": classification_data.get('suggested_title'),
        "suggested_project": classification_data.get('suggested_project')
    }


async def main():
    """Standalone entry point for GitHub Actions (polling mode)."""
    print(f"WhatsApp ingest started at {datetime.now(timezone(timedelta(hours=5, minutes=30)))}")
    print("Polling mode not yet configured — use POST /api/whatsapp-ingest for real-time.")
    print("WhatsApp ingest complete. 0 processed.")


if __name__ == "__main__":
    asyncio.run(main())
