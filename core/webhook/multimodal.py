from datetime import datetime, timezone, timedelta
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.webhook.telegram import send_telegram
from core.webhook.classify import call_gemini_with_retry, CLASSIFICATION_MODEL, classify_intent
from core.webhook.dispatch import route_by_intent


async def process_multimodal_content(file_bytes: bytes, mime_type: str, chat_id: int, ist_hour: int = None, core_json: str = "[]"):
    """Two-pass extraction: verbatim OCR → standard text pipeline."""
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour

    try:
        # ── Pass 1: Pure verbatim extraction ──
        extraction_prompt = "Transcribe ALL visible text from this image exactly as shown. Preserve original line breaks, spacing, and punctuation. Do not summarize, normalize, or omit any content. Return ONLY the raw text — no explanations, no formatting."
        parts = [genai.types.Part(text=extraction_prompt)]

        if mime_type.startswith('image/'):
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif mime_type.startswith('audio/') or mime_type == 'application/octet-stream':
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif mime_type in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        else:
            parts.append(genai.types.Part(text=file_bytes.decode('utf-8', errors='ignore')))

        content_parts = [genai.types.Content(parts=parts)]

        response = await call_gemini_with_retry(
            extraction_prompt,
            contents=content_parts,
            model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'text/plain'}
        )
        raw_text = response.text.strip()

        if not raw_text:
            await send_telegram(chat_id, "Couldn't extract any text from that.")
            return

        # ── Pass 2: Feed into standard text pipeline ──
        classification = await classify_intent(
            raw_text,
            context=[],
            ist_hour=current_hour,
            core_json=core_json
        )

        intent = classification.get('intent', 'NOTE')
        confidence = classification.get('confidence', 0.5)
        source = "multimodal"

        CONFIDENCE_LOW = 0.5

        if confidence >= CONFIDENCE_LOW:
            await route_by_intent(
                intent, raw_text, chat_id, session_id=None,
                classification=classification, source=source
            )
        else:
            from core.webhook.dispatch import handle_confident_note
            await handle_confident_note(raw_text, chat_id, source=source)

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Multimodal processing error: {e}")
        ack = "Something went wrong. Try sending as text."
        await send_telegram(chat_id, f"\u26a0\ufe0f {ack}")
