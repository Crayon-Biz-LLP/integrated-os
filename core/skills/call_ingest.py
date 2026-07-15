import os
import json
import io
import tempfile
import asyncio
from datetime import datetime, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from faster_whisper import WhisperModel # type: ignore
from core.services.db import get_supabase
from core.services.llm import call_gemini_classify, CLASSIFICATION_MODEL
from core.services.google_service import get_google_creds


GOOGLE_DRIVE_CALLS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_CALLS_FOLDER_ID")

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

supabase = get_supabase()


def get_drive_service():
    return build("drive", "v3", credentials=get_google_creds())


def list_new_recordings(service, folder_id: str) -> list:
    processed = set()
    existing = supabase.table("call_recordings").select("drive_file_id").execute()
    for row in (existing.data or []):
        processed.add(row["drive_file_id"])

    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, size, createdTime)",
        orderBy="createdTime desc",
        pageSize=10
    ).execute()

    raw_files = results.get("files", []) or []
    print(f"  Found {len(raw_files)} file(s) in Drive folder")
    for f in raw_files:
        print(f"    {f['name']} ({f['mimeType']})")

    new_files = [f for f in raw_files if f["id"] not in processed]
    if processed:
        print(f"  Filtered to {len(new_files)} new file(s) (ignoring {len(processed)} already processed)")
    return new_files


def download_audio(service, file_id: str) -> tuple:
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    name_res = service.files().get(fileId=file_id, fields="name").execute()
    return fh.getvalue(), name_res.get("name", "unknown")


def transcribe(audio_bytes: bytes) -> tuple:
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        segments, info = model.transcribe(tmp_path, language="en", beam_size=5)
        transcript = " ".join(seg.text for seg in segments)
        return transcript, info.duration
    finally:
        os.unlink(tmp_path)


async def extract_with_gemini(transcript: str) -> dict:
    prompt = f"""Extract structured information from this call transcript.

Return valid JSON only (no markdown, no explanation):
{{
  "summary": "2-3 sentence summary of the call",
  "duration_minutes": <estimated int>,
  "action_items": [
    {{"task": "verb-first task description", "project": "SOLVSTRAT|QHORD|ASHRAYA|PERSONAL|CRAYON|INBOX"}}
  ],
  "key_decisions": ["key decisions made"],
  "people_mentioned": ["person names"],
  "has_memory_value": true or false
}}

Project routing rules:
- SOLVSTRAT: tech, client work, delivery
- QHORD: product GTM, launch (June 2026)
- ASHRAYA: church admin, operations
- PERSONAL: family, home, health, spiritual
- CRAYON: company governance, legal, tax
- INBOX: if unsure

Transcript:
{transcript[:15000]}"""

    response = await call_gemini_classify(
        prompt=prompt,
        model=CLASSIFICATION_MODEL,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


def send_telegram_notification(message: str):
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        print("Missing TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN — skipping notification")
        return
    import httpx
    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    )
    if resp.status_code != 200:
        print(f"Telegram notification failed: {resp.text}")


async def process_recordings():
    folder_id = GOOGLE_DRIVE_CALLS_FOLDER_ID
    if not folder_id:
        print("ERROR: GOOGLE_DRIVE_CALLS_FOLDER_ID not set")
        return

    print(f"Checking Google Drive folder {folder_id} for new recordings...")
    drive = get_drive_service()
    new_files = list_new_recordings(drive, folder_id)

    if not new_files:
        print("No new recordings found.")
        return

    total_tasks = 0
    total_decisions = 0
    processed_count = 0

    for f in new_files:
        file_id = f["id"]
        file_name = f.get("name", "unknown")
        mime = f.get("mimeType", "audio/mpeg")
        size = f.get("size")
        print(f"Processing: {file_name} ({file_id})")

        try:
            audio_bytes, _ = download_audio(drive, file_id)
            print(f"  Downloaded {len(audio_bytes)} bytes — transcribing...")

            transcript, duration = transcribe(audio_bytes)
            dur_mins = int(duration // 60) if duration else 0
            print(f"  Transcribed {dur_mins}min — extracting...")

            extraction = await extract_with_gemini(transcript)

            recording_data = {
                "drive_file_id": file_id,
                "drive_file_name": file_name,
                "mime_type": mime,
                "file_size_bytes": int(size) if size else None,
                "duration_seconds": int(duration) if duration else None,
                "transcript": transcript,
                "extraction": json.dumps(extraction) if isinstance(extraction, dict) else extraction,
                "status": "completed",
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
            rec_res = supabase.table("call_recordings").insert(recording_data).execute()
            recording_id = rec_res.data[0]["id"] if rec_res.data else None
            if not recording_id:
                print("  Failed to insert recording record — skipping items")
                continue

            # Route through ingest() — one call per action item/decision
            from core.lib.ingest import ingest
            item_count = 0
            for ai in extraction.get("action_items", []):
                task = ai.get("task", "").strip()
                if task:
                    await ingest(
                        text=task,
                        source='call_recording',
                        classification='actionable',
                        summary=extraction.get('summary', '')[:500],
                        suggested_title=task,
                        suggested_project=ai.get('project'),
                        channel_specific_data={
                            'action_type': 'task',
                            'people_mentioned': extraction.get('people_mentioned', []),
                            'recording_id': recording_id,
                        },
                    )
                    item_count += 1
            for kd in extraction.get("key_decisions", []):
                kd_text = kd.strip()
                if kd_text:
                    await ingest(
                        text=kd_text,
                        source='call_recording',
                        classification='actionable',
                        summary=extraction.get('summary', '')[:500],
                        suggested_title=kd_text,
                        channel_specific_data={
                            'action_type': 'decision',
                            'people_mentioned': extraction.get('people_mentioned', []),
                            'recording_id': recording_id,
                        },
                    )
                    item_count += 1
            if extraction.get("has_memory_value") and extraction.get("summary"):
                summary_text = extraction['summary']
                await ingest(
                    text=summary_text,
                    source='call_recording',
                    classification='fyi',
                    summary=summary_text[:500],
                    has_memory_value=True,
                    is_human_sender=True,
                    channel_specific_data={
                        'action_type': 'note',
                        'people_mentioned': extraction.get('people_mentioned', []),
                        'recording_id': recording_id,
                    },
                )
                item_count += 1

            task_count = sum(1 for ai in extraction.get("action_items", []) if ai.get('task', '').strip())
            decision_count = sum(1 for kd in extraction.get("key_decisions", []) if kd.strip())
            total_tasks += task_count
            total_decisions += decision_count
            processed_count += 1

            print(f"  ✓ {file_name}: {task_count} tasks, {decision_count} decisions")

        except Exception as e:
            print(f"  ✗ Failed to process {file_name}: {e}")
            supabase.table("call_recordings").insert({
                "drive_file_id": file_id,
                "drive_file_name": file_name,
                "mime_type": mime,
                "status": "failed",
                "error_message": str(e)
            }).execute()

    if processed_count > 0:
        msg = (
            f"📞 *Call Processing Report*\n"
            f"Processed *{processed_count}* recording(s)\n"
            f"→ {total_tasks} action item(s)\n"
            f"→ {total_decisions} decision(s)\n"
            f"Review in your next briefing."
        )
        send_telegram_notification(msg)
        print(f"Notification sent: {processed_count} recordings processed")


async def main():
    await process_recordings()


if __name__ == "__main__":
    asyncio.run(main())
