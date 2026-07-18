"""Multimodal content processing: hybrid extraction → standard text pipeline.

Architecture:
  - Documents (PDF, DOCX, XLSX, PPTX) → local extraction (50ms, free, verbatim)
  - Images → Gemini vision (OCR for scanned docs, photos)
  - Audio → Gemini audio transcription (unchanged)
  - Fallback → Gemini Flash Lite via direct API call (no wrapper duplication)
"""

from datetime import datetime
from core.lib.time_utils import IST_TIMEZONE
from google import genai
from core.lib.audit_logger import audit_log_sync
from core.lib.document_extractor import extract_text
from core.webhook.telegram import send_telegram
from core.webhook.classify import classify_intent
from core.llm.constants import SYNTHESIS_MODEL
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
        return 'audio/mp4'

    return default_mime


async def _extract_via_gemini(file_bytes: bytes, mime_type: str, is_audio: bool) -> str:
    """Fallback extraction using Gemini vision/audio.

    This is the OLD path — kept for images and audio only.
    Documents (PDF, DOCX, XLSX, PPTX) use local extraction now.
    """
    if is_audio:
        extraction_prompt = (
            "Transcribe this audio message exactly as spoken. "
            "Do not summarize, normalize, or omit any content. "
            "Return ONLY the raw text — no explanations, no formatting."
        )
    else:
        extraction_prompt = (
            "Transcribe ALL visible text from this image exactly as shown. "
            "Preserve original line breaks, spacing, and punctuation. "
            "Do not summarize, normalize, or omit any content. "
            "Return ONLY the raw text — no explanations, no formatting."
        )

    # Build content parts: prompt + file bytes
    parts = [genai.types.Part(text=extraction_prompt)]

    if mime_type.startswith('image/'):
        parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    elif mime_type.startswith('audio/'):
        parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    elif mime_type in ('application/pdf',):
        parts.append(genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    else:
        # Fallback: send as text
        try:
            text_content = file_bytes.decode('utf-8', errors='ignore')
            parts.append(genai.types.Part(text=text_content))
        except Exception:
            return ""

    content_parts = [genai.types.Content(parts=parts)]

    # Direct API call — prompt is ONLY in contents, NOT duplicated
    response = await generate_content_with_fallback(
        prompt="",  # Empty — instruction is in contents[0].parts[0]
        workload=WorkloadProfile.INTERACTIVE,
        contents=content_parts,
        primary_model=SYNTHESIS_MODEL,
        config={'response_mime_type': 'text/plain'}
    )

    raw_text = response.text.strip() if response and response.text else ""
    return raw_text


async def process_multimodal_content(
    file_bytes: bytes, mime_type: str, chat_id: int,
    ist_hour: int = None, core_json: str = "[]"
):
    """Two-pass processing: extract → classify → route.

    Pass 1: Hybrid extraction (local for docs, Gemini for images/audio)
    Pass 2: Standard text pipeline (classify → plan → execute)
    """
    ist_offset = IST_TIMEZONE
    now = datetime.now(ist_offset)
    current_hour = ist_hour if ist_hour is not None else now.hour

    try:
        # ── MIME resolution ──
        if mime_type == 'application/octet-stream' or not mime_type:
            mime_type = guess_mime_type(file_bytes, mime_type)

        is_audio = mime_type and mime_type.startswith('audio/')
        extraction_method = "voice_memo" if is_audio else "alt_image"

        # ── Pass 1: Extract text ──
        raw_text = ""

        # Try local extraction first (PDF, DOCX, XLSX, PPTX, text)
        if not is_audio:
            extracted = extract_text(file_bytes, mime_type)
            if extracted:
                raw_text = extracted
                extraction_method = "document_extract"

        # Fall back to Gemini for images, audio, or failed local extraction
        if not raw_text:
            raw_text = await _extract_via_gemini(file_bytes, mime_type, is_audio)

        if not raw_text:
            await send_telegram(chat_id, "Couldn't extract any text from that.")
            return

        # Tag extraction source for downstream transparency
        if not is_audio and extraction_method == "document_extract":
            tagged = raw_text
        elif not is_audio:
            tagged = f"ALT IMAGE: {raw_text}"
        else:
            tagged = raw_text

        # ── Pass 2: Feed into standard text pipeline ──
        classification = await classify_intent(
            tagged,
            context=[],
            ist_hour=current_hour,
            core_json=core_json,
        )
        classification["extraction_method"] = extraction_method

        intent = classification.get('intent', 'NOTE')
        confidence = classification.get('confidence', 0.5)
        source = "multimodal"

        CONFIDENCE_LOW = 0.5

        if confidence >= CONFIDENCE_LOW:
            await route_by_intent(
                intent, tagged, chat_id, session_id=None,
                classification=classification, source=source,
            )
        else:
            from core.lib.ingest import ingest
            await ingest(
                text=tagged,
                source="multimodal",
                classification="note",
                has_memory_value=True,
                channel_specific_data={
                    "extraction_method": extraction_method,
                },
            )

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Multimodal processing error: {e}")
        ack = "Something went wrong. Try sending as text."
        await send_telegram(chat_id, f"\u26a0\ufe0f {ack}")
