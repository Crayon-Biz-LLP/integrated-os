from datetime import datetime, timezone, timedelta
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.webhook.telegram import send_telegram
from core.webhook.classify import CLASSIFICATION_MODEL, classify_intent
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.webhook.dispatch import route_by_intent


def guess_mime_type(file_bytes: bytes, default_mime: str) -> str:
    """Guess MIME type from file signatures (magic numbers) for common formats."""
    if not file_bytes:
        return default_mime
        
    if file_bytes.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    elif file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    elif file_bytes.startswith(b'GIF87a') or file_bytes.startswith(b'GIF89a'):
        return 'image/gif'
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WEBP':
        return 'image/webp'
    elif file_bytes.startswith(b'%PDF-'):
        return 'application/pdf'
    elif file_bytes.startswith(b'OggS'):
        return 'audio/ogg'
    elif file_bytes.startswith(b'ID3') or file_bytes.startswith(b'\xff\xfb') or file_bytes.startswith(b'\xff\xf3') or file_bytes.startswith(b'\xff\xf2'):
        return 'audio/mpeg'
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WAVE':
        return 'audio/wav'
    elif len(file_bytes) > 8 and file_bytes[4:8] == b'ftyp':
        # common for both video/mp4 and audio/mp4(m4a)
        # Gemini handles 'audio/mp4' well for audio files
        return 'audio/mp4'
        
    return default_mime


async def process_multimodal_content(file_bytes: bytes, mime_type: str, chat_id: int, ist_hour: int = None, core_json: str = "[]"):
    """Two-pass extraction: verbatim OCR → standard text pipeline."""
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour

    try:
        # ── Pass 1: Pure verbatim extraction ──
        if mime_type == 'application/octet-stream' or not mime_type:
            mime_type = guess_mime_type(file_bytes, mime_type)

        is_audio = mime_type and mime_type.startswith('audio/')
        
        if is_audio:
            extraction_prompt = "Transcribe this audio message exactly as spoken. Do not summarize, normalize, or omit any content. Return ONLY the raw text — no explanations, no formatting."
        else:
            extraction_prompt = "Transcribe ALL visible text from this document or image exactly as shown. Preserve original line breaks, spacing, and punctuation. Do not summarize, normalize, or omit any content. Return ONLY the raw text — no explanations, no formatting."

        parts = [genai.types.Part(text=extraction_prompt)]

        if mime_type.startswith('image/'):
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif mime_type.startswith('audio/'):
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif mime_type in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif mime_type == 'application/octet-stream':
            # Still octet-stream even after guess? Try decoding as text, or send a generic failure
            try:
                text_content = file_bytes.decode('utf-8')
                parts.append(genai.types.Part(text=text_content))
            except UnicodeDecodeError:
                await send_telegram(chat_id, "Unsupported file format (couldn't infer type).")
                return
        else:
            parts.append(genai.types.Part(text=file_bytes.decode('utf-8', errors='ignore')))

        content_parts = [genai.types.Content(parts=parts)]

        response = await generate_content_with_fallback(
            prompt=extraction_prompt,
            workload=WorkloadProfile.INTERACTIVE,
            contents=content_parts,
            primary_model=CLASSIFICATION_MODEL,
            config={'response_mime_type': 'text/plain'}
        )
        raw_text = response.text.strip()

        if not raw_text:
            await send_telegram(chat_id, "Couldn't extract any text from that.")
            return

        if not is_audio:
            raw_text = f"ALT IMAGE: {raw_text}"

        # ── Pass 2: Feed into standard text pipeline ──
        classification = await classify_intent(
            raw_text,
            context=[],
            ist_hour=current_hour,
            core_json=core_json
        )
        extraction_method = "voice_memo" if is_audio else "alt_image"
        classification["extraction_method"] = extraction_method

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
            from core.lib.ingest import ingest
            await ingest(
                text=raw_text,
                source="multimodal",
                classification="note",
                has_memory_value=True,
                channel_specific_data={"extraction_method": extraction_method}
            )

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Multimodal processing error: {e}")
        ack = "Something went wrong. Try sending as text."
        await send_telegram(chat_id, f"\u26a0\ufe0f {ack}")
