# Integrated-OS Agent Guide

## Project Overview
FastAPI-based executive command system deployed as Vercel serverless functions (Python 3.11, matches CI). Processes Telegram messages into tasks, syncs with Google Calendar/Tasks, sends AI-generated briefings via Telegram.

## Key Commands

### Local Development
```bash
pip install -r requirements.txt
pip install uvicorn  # Not in requirements.txt
uvicorn api.index:app --reload --port 8000
```

### Pulse CLI (Local)
```bash
python core/pulse_cli.py  # Requires PULSE_SECRET, Supabase, Gemini, Telegram vars
```

### Deployment
Vercel auto-deploys `main` branch. All routes rewritten to `api/index.py` (see `vercel.json`). Serverless function timeout: 60s.

## Architecture

### Entry Points
- `api/index.py:29` - POST `/api/webhook` - Telegram message intake
- `api/index.py:44` - POST `/api/pulse` - Scheduled briefing engine
- `core/pulse_cli.py` - CLI entry for pulse (used in CI)

### Core Modules
- `core/webhook.py` - Telegram command handling, raw dump capture, message classification
- `core/pulse.py` - AI briefing generation, task management, calendar sync. `format_rfc3339()` at line 1024
- `core/research_agent.py` - Research and embedding tasks
- `core/skills/` - Ingest (email, archive) and graph sync scripts (run via CI)

### Database (Supabase)
- Uses `SUPABASE_SERVICE_ROLE_KEY` (bypasses RLS)
- Tables: `tasks`, `raw_dumps`, `projects`, `resources`, `missions`, `people`, `core_config`

### External Integrations
- **Gemini AI**: Briefing (`gemini-3-flash-preview`), Classification (`gemini-3.1-flash-lite-preview`), Embeddings (`gemini-embedding-2-preview`)
- Google Calendar API (event blocks), Google Tasks API (checklist)
- Telegram Bot API

## Project Routing Tags
| Tag | Purpose |
|-----|---------|
| SOLVSTRAT | Cash engine, client work |
| PRODUCT_LABS | Incubator projects (CashFlow+, Integrated-OS) |
| CRAYON | Governance, tax, legal |
| PERSONAL | Family, home |
| CHURCH | Church activities |

## Critical Conventions

### Time Handling
- All timestamps use **IST (UTC+05:30)**
- Use `format_rfc3339()` in `core/pulse.py:1024` to sanitize times
- Format: `YYYY-MM-DDTHH:MM:SS+05:30`

### Security
- Pulse endpoints validate `PULSE_SECRET` (header `x-pulse-secret`) and HMAC `X-Rhodey-Signature`
- Supabase uses service role key (bypasses RLS)

### Pulse Cron Schedule (UTC, matches `.github/workflows/pulse.yml`)
- Weekdays: `0 2,7,11,14 * * 1-5` (7:30AM, 12:30PM, 4:30PM, 7:30PM IST)
- Weekends: `30 4,9 * * 0,6` (10AM, 3PM IST)

### AI Briefing Rules
- NEVER create tasks from URLs unless explicitly commanded
- NEVER mark tasks done unless input explicitly matches
- Return empty arrays if no explicit commands in inputs
- Filter tasks by 2-day horizon, 14-day creation window

## Required Environment Variables
```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
PULSE_SECRET
GOOGLE_REFRESH_TOKEN
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_SHEET_ID  # Used in archive ingest and pulse
```

## Testing
- CI: GitHub Actions (`workflow_dispatch` in `.github/workflows/pulse.yml`)
- Local: Send POST to `/api/pulse` with header `x-pulse-secret: <PULSE_SECRET>`
- No linters/typecheckers configured; skip lint/typecheck steps
