# WhatsApp Ingest

## Overview
Ingests WhatsApp messages as a new input channel, separate from the email pipeline. Uses Android's notification listener (via MacroDroid) to capture messages and forward them to a Vercel endpoint for Gemini classification and approval routing.

## Data Flow
```
WhatsApp notification on Android phone
       ↓
MacroDroid trigger (reads sender + text)
       ↓
HTTP POST → POST /api/whatsapp-ingest (Vercel Python)
  Header: X-Ingest-Secret: <shared key>
  Body: { sender, phone, text, received_at }
       ↓
core/skills/whatsapp_ingest.py::process_whatsapp_message()
  1. Dedup check (by phone + text within 24h)
  2. Gemini classification (actionable/fyi/ignored)
  3. Insert into messages table
  4. If actionable → pending for Danny's review
  5. If fyi → optional memories write
  6. If ignored → skipped
```

## Database Table

### `messages`
| Column | Type | Purpose |
|--------|------|---------|
| `id` | int8 (PK) | Shortcode: `w{id}` |
| `sender_name` | text? | Contact name (if saved on phone) |
| `sender_phone` | text | Phone number |
| `message_text` | text | Raw message content |
| `classification` | text? | `actionable`, `fyi`, `ignored` |
| `summary` | text? | Gemini-generated summary |
| `suggested_title` | text? | Verb-first task suggestion (if actionable) |
| `suggested_project` | text? | Project tag from Gemini |
| `linked_person_name` | text? | Identified person |
| `has_memory_value` | bool | For FYI → memories |
| `shown_in_brief` | bool | Pulse delivery flag |
| `danny_decision` | text? | `approved`, `rejected`, `expired`, `skipped` |
| `decided_at` | timestamptz? | When approved/rejected |
| `received_at` | timestamptz | Message timestamp |
| `created_at` | timestamptz | Row creation |

## Classification Prompt
Uses WhatsApp-specific context: personal chats, family, friends, church contacts. Same Gemini model (`gemini-3.1-flash-lite-preview`) and JSON-structured output as email classification, but adapted for shorter, conversational WhatsApp messages.

## Approval Flow

### Decision Pulse (Standalone, No AI)
Pending actionable WhatsApp messages appear in the **Decision Pulse** — a separate Telegram message (no AI, runs on every cron trigger) — under the **💬 WhatsApp Extracts** section, formatted as:
```
💬 WHATSAPP EXTRACTS (3) — reply [w{code}] yes/drop
💬 [w{id}] Call Amma about Sunday lunch (PERSONAL) — Amma
```
The Decision Pulse is not part of the main AI briefing. It runs independently via `process_decision_pulse()` — fetches pending items from `messages`, `messages`, and `messages`, formats with shortcodes, sends to Telegram. No AI generation, ~2s runtime.

### Telegram Shortcodes
| Command | Action |
|---------|--------|
| `w{id} yes` | Creates raw_dump → Pulse picks up as task |
| `w{id} drop` | Rejects, sets danny_decision='rejected' |

Prefixed shortcodes (`w{id}`) route directly to `messages` via `core/webhook/whatsapp.py`. Unprefixed shortcodes (`{id}`) fall back through email → call → WhatsApp → practice dismissal.

### Decision Handler (`core/webhook/whatsapp.py`)
- **Approve**: Inserts into `raw_dumps` with `source="whatsapp"` and metadata (sender, phone, summary). Next Pulse cycle classifies and creates tasks.
- **Reject**: Sets `danny_decision='rejected'`, records `decided_at`.

## API Endpoint
**`POST /api/whatsapp-ingest`**
- Auth: `X-Ingest-Secret` header (matches `WHATSAPP_INGEST_SECRET` env var)
- Body: `{ sender, phone, text, received_at }`
- Response: `{ success: true, result: { status, classification, ... } }`

## Environment Variables
| Variable | Purpose |
|----------|---------|
| `WHATSAPP_INGEST_SECRET` | Shared secret between MacroDroid and Vercel |

## Code Files
| File | Purpose |
|------|---------|
| `core/skills/whatsapp_ingest.py` | Classification, dedup, DB insert logic |
| `core/webhook/whatsapp.py` | Decision handler (approve/reject → raw_dumps) |
| `api/index.py` | `POST /api/whatsapp-ingest` route |
| `core/webhook/handler.py` | `w{id}` shortcode routing |
| `core/pulse/engine.py` | `process_decision_pulse()` — standalone decision pulse (no AI) |

## Key Design Decisions
1. **Separate table** (`messages`) rather than reusing `messages` — keeps WhatsApp independent from the email pipeline
2. **Single table** rather than `recordings` + `pending_items` — WhatsApp messages are atomic (one message = one item), unlike calls which have multiple action items per recording
3. **No email_drafts equivalent** — WhatsApp doesn't support draft-and-send-back workflows via this channel, but could be extended later
4. **Phone-notification based** via MacroDroid rather than browser automation (OpenWA) — zero infra, no ToS risk, always connected
