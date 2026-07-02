# Call Recording Ingestion

## Overview
The Call Recording Ingestion pipeline automates the transcription and extraction of action items from voice recordings. It bridges Google Drive, a local `faster-whisper` transcription model, and the Gemini AI to convert raw audio into structured tasks and memories. 

## Architecture & Data Flow

1. **Recording Upload**:
   - The user records a call (e.g., via a phone dialer app or voice recorder) and saves/syncs it to a specific Google Drive folder.
   
2. **Drive Webhook Notification**:
   - Google Drive sends a push notification to `/api/drive-webhook` whenever a new file is added to the watched folder.
   - *Note:* The webhook push channel is periodically renewed by a GitHub Actions cron job (`.github/workflows/renew_drive_channel.yml` running `core/skills/renew_drive_channel.py`).

3. **Transcription & Extraction (`core/skills/call_ingest.py`)**:
   - A GitHub Actions workflow (`.github/workflows/call_ingest.yml`) is triggered to process new recordings.
   - Unprocessed files are downloaded from Google Drive into memory.
   - The audio is locally transcribed using `faster-whisper` (defaulting to the `base` model).
   - The resulting text transcript is sent to the Gemini API (`call_gemini_classify`) for structured extraction.

4. **Structured AI Output**:
   Gemini parses the transcript into a JSON payload:
   - Summary of the call
   - Action Items (with project routing tags)
   - Key Decisions
   - People Mentioned
   - Memory Value flag

5. **Database Storage**:
   - The raw recording metadata and summary are saved to the `call_recordings` table.
   - Extracted tasks are inserted into the `messages` table with a `pending` status.

6. **Decision Pulse (User Approval)**:
   - The Decision Pulse engine (`process_decision_pulse()`) picks up the pending items and sends them to Telegram alongside email and WhatsApp decisions.
   - The items appear with shortcodes, e.g., `[c12] Call plumber (PERSONAL)`.
   - The user replies via Telegram (e.g., `c12 yes` or `c12 drop`).

7. **Webhook Resolution**:
   - Telegram replies hit `/api/webhook` and are handled by `core/webhook/call.py`.
   - Approvals format the task text and push it into the `raw_dumps` table.
   - On the next pulse run, the task is properly routed, assigned, and synchronized with Google Tasks/Calendar just like any other Telegram task.

## Database Schema

### `call_recordings`
Stores the metadata for processed audio files to ensure idempotency.
| Column | Type | Purpose |
|--------|------|---------|
| `id` | int8 (PK) | Auto-incrementing ID |
| `drive_file_id` | text | Unique identifier from Google Drive, used for deduping |
| `file_name` | text | Original file name |
| `transcript` | text | Full `faster-whisper` transcription |
| `summary` | text | Gemini-generated summary |
| `duration_seconds` | int4 | Audio duration |
| `created_at` | timestamptz | Ingestion time |

### `messages`
Stores the extracted tasks waiting for user approval.
| Column | Type | Purpose |
|--------|------|---------|
| `id` | int8 (PK) | Shortcode: `c{id}` |
| `recording_id` | int8 (FK) | Links back to the source recording |
| `task_text` | text | Extracted action item |
| `project` | text | Suggested project tag (e.g., SOLVSTRAT) |
| `danny_decision` | text | `approve`, `reject`, or null (pending) |
| `created_at` | timestamptz | Extraction time |

## Desktop Meeting Recording (macOS — Meetily)

### Overview
Desktop meetings (Zoom, Meet, Teams) are captured via **Meetily** — an open-source, privacy-first meeting assistant that records mic + system audio locally on your MacBook. The raw `.mp4` files are automatically synced to the same Google Drive folder monitored by `call_ingest.py`, so the rest of the pipeline is identical to phone call recordings.

### Architecture

```
Zoom/Meet/Teams call (on MacBook)
              │
              ▼
Meetily (menu bar app)
  └── Records mic + system audio → saves subfolder with:
        ├── recording.mp4
        ├── metadata.json  (skipped — not synced)
        └── transcripts.json (skipped — not synced)
              │
              ▼
meetily-sync.sh (launchd, runs every 2 min)
  └── Finds all *.mp4 in subfolders, renames by folder name,
      copies flat to Google Drive:
        rhodey-calls:Crayon/Rhodey OS/Call Recordings/
              │
              ▼
Existing call_ingest pipeline (no changes)
```

### Setup

| Component | Location | Purpose |
|-----------|----------|---------|
| **Meetily** | `~/Applications/Meetily.app` | Desktop meeting recorder (GitHub: Zackriya-Solutions/meetily) |
| **rclone** | `/opt/homebrew/bin/rclone` | Synces `.mp4` from local folder to Google Drive |
| **Sync script** | `~/meetily-sync.sh` | Finds all `.mp4` files in Meetily's subfolders, renames by folder name (unique), copies flat to Drive |
| **launchd plist** | `~/Library/LaunchAgents/com.meetily.drive.sync.plist` | Runs the sync script every 2 minutes |
| **Drive folder** | `Crayon/Rhodey OS/Call Recordings` (ID: `1gc_1w33Di7W3JkONiYg_Ie2LBujs47ad`) | Target folder monitored by `call_ingest.py` |

### How Recording Works

1. Open Meetily from menu bar → click **Start Recording**
2. Meetily captures both mic input + system audio (so all meeting participants are heard)
3. After the meeting, click **Stop Recording**
4. Meetily saves to `~/Movies/meetily-recordings/Meeting YYYY-MM-DD_HH-MM-SS_.../audio.mp4`
5. Within 2 minutes, `meetily-sync.sh` picks it up, renames it to the folder name, and uploads to Drive
6. Within 30 minutes, the GitHub Actions cron (`call_ingest.yml`) processes it through the existing pipeline
7. Action items appear in the Decision Pulse on Telegram

### File Naming

Meetily names all recordings `audio.mp4` inside their respective folders. The sync script renames them to the parent folder name before uploading:

```
Before:  ~/Movies/meetily-recordings/Meeting 2026-07-02_17-17-53_.../audio.mp4
After:   Drive: Crayon/Rhodey OS/Call Recordings/Meeting 2026-07-02_17-17-53_....mp4
```

This ensures unique filenames. Only `.mp4` files are synced — `metadata.json` and `transcripts.json` remain local.

### Management Commands

| Action | Command |
|--------|---------|
| View running watcher | `launchctl list \| grep meetily` |
| Stop the watcher | `launchctl unload ~/Library/LaunchAgents/com.meetily.drive.sync.plist` |
| Restart the watcher | `launchctl load ~/Library/LaunchAgents/com.meetily.drive.sync.plist` |
| Check sync logs | `cat ~/Library/Logs/meetily-drive-sync.log` |
| Run sync now | `bash ~/meetily-sync.sh` |
| Verify Drive files | `rclone ls rhodey-calls:Crayon/Rhodey\ OS/Call\ Recordings` |

### Why This Approach

- **No code changes needed** — Meetily just replaces "phone dialer app" with "desktop meeting recorder" as the audio source
- **Privacy-first** — Meetily runs locally, no cloud transcription. Audio is only sent to Rhodey's pipeline after recording ends
- **Zero API cost** — Uses free Google Drive storage and the existing faster-whisper/Gemini pipeline
- **No new infrastructure** — No new Vercel endpoints, no new tables, no new GitHub Actions workflows

## Security & Reliability
- **Idempotency**: The system queries the `call_recordings` table by `drive_file_id` before processing to guarantee a file is only transcribed once.
- **Webhook Expiry**: Google Drive push channels expire. The `renew_drive_channel.yml` action runs dynamically to keep the webhook active without manual intervention.
- **Local Transcription**: Uses `faster-whisper` running on CPU (`int8` compute type) within the GitHub Action runner to keep processing costs low while preserving privacy for raw audio.