from core.llm.constants import CLASSIFICATION_MODEL
from core.llm import get_embedding
import json
import asyncio
from datetime import datetime, timezone, timedelta
from core.retrieval.pipeline import schedule_index_memory
from core.pulse.entity_extractor import extract_and_link_entities
from core.services.db import get_supabase
from core.services.llm import call_gemini_classify
from core.lib.time_utils import resolve_expiry

supabase = get_supabase()

NOREPLY_PATTERNS = [
    'noreply', 'no-reply', 'donotreply', 'notification',
    'alert', 'bot', 'automated', 'service'
]


async def classify_whatsapp_message(sender_name: str, sender_phone: str, message_text: str) -> dict:
    prompt = f"""You are classifying a WhatsApp message for Danny (Yashwant Daniel).

MAILBOX CONTEXT: This is Danny's PERSONAL WhatsApp. It receives messages from family, friends, church contacts, and personal relationships. Work-related messages (clients, vendors, team) should be treated as actionable.

Sender: {sender_name or sender_phone}
Message: {message_text[:1000]}

CLASSIFICATION RULES

CLASSIFY AS "ignored" IF ANY:
- Automated or service message (OTP, notification, delivery update, payment alert)
- Group broadcast or mass-forwarded message with no personal context
- Promotional or spam message from an unknown number

CLASSIFY AS "fyi" IF:
- A real person sharing information, updates, or casual conversation — no response needed
- A forwarded message worth noting but requiring no action

CLASSIFY AS "actionable" IF:
- A real person asking Danny to do something, respond, decide, coordinate, or take action
- A request related to family, church, work, or personal obligations
- When in doubt, surface it as actionable

OUTPUT RULES

suggested_title:
- Verb-first, specific action (e.g., "Call Amma about Sunday lunch", "Review Qhord pricing page", "Confirm prayer meeting time with Elder Thomas")
- NULL if fyi or ignored
- NULL if action cannot be stated specifically

suggested_project:
- One of: SOLVSTRAT, QHORD, ASHRAYA, PERSONAL, CRAYON, INBOX
- NULL if unsure

linked_person_name:
- Full name of the person mentioned or sending the message if identifiable
- NULL if unknown

has_memory_value:
- true if the message contains a decision, commitment, relationship context, or information worth remembering weeks later
- false for routine or trivial chat

Return ONLY valid JSON, NO markdown, NO explanation:
{{
  "classification": "ignored|fyi|actionable",
  "summary": "1-2 sentences. Who sent it, what they want or shared.",
  "suggested_title": "verb-first task or null",
  "suggested_project": "project tag or null",
  "linked_person_name": "name or null",
  "has_memory_value": true or false
}}"""

    response = await call_gemini_classify(
        prompt,
        model=CLASSIFICATION_MODEL,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


async def process_whatsapp_message(sender_name: str, sender_phone: str, message_text: str, received_at: str = None) -> dict:
    print(f"Processing WhatsApp message from {sender_name or sender_phone}: {message_text[:60]}...")

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

    if any(p in (sender_name or sender_phone).lower() for p in NOREPLY_PATTERNS):
        classification_data = {
            "classification": "ignored", "summary": "Automated or no-reply sender",
            "suggested_title": None, "suggested_project": None,
            "linked_person_name": None, "has_memory_value": False
        }
    else:
        try:
            classification_data = await classify_whatsapp_message(sender_name, sender_phone, message_text)
        except Exception as e:
            print(f"Classification failed for {sender_phone}: {e}")
            classification_data = {
                "classification": "ignored", "summary": "Classification error",
                "suggested_title": None, "suggested_project": None,
                "linked_person_name": None, "has_memory_value": False
            }

    classification = classification_data.get('classification', 'ignored')

    if classification == 'ignored':
        row = {
            "channel": "whatsapp",
            "source": "whatsapp",
            "sender_name": sender_name or sender_phone,
            "sender_id": sender_phone,
            "body": message_text.strip(),
            "classification": classification,
            "summary": classification_data.get('summary', ''),
            "suggested_title": classification_data.get('suggested_title'),
            "suggested_project": classification_data.get('suggested_project'),
            "has_memory_value": classification_data.get('has_memory_value', False),
            "received_at": now_iso,
            "processing_status": "completed",
            "metadata": {
                "sender_phone": sender_phone
            },
            "danny_decision": "skipped",
            "expires_at": expires_iso
        }
        supabase.table('messages').insert(row).execute()
        print(f"[ignored] {sender_name or sender_phone}: {message_text[:60]}")
        return {"status": "ignored", "classification": classification}

    # Actionable and FYI: Atomically batch or insert via RPC
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
            extract_and_link_entities(mem_content, str(memory_id), 'memory')
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
