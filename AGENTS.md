# Integrated-OS Agent Guide

## Project Overview
FastAPI-based executive command system deployed on Vercel. Processes Telegram messages into tasks, syncs with Google Calendar/Tasks, and sends AI-generated briefings via Telegram.

## Key Commands

### Local Development
```bash
pip install -r requirements.txt
pip install uvicorn
uvicorn api.index:app --reload --port 8000
```

### Deployment
Vercel auto-deploys from main branch. All routes in `vercel.json`:
- `/` → `index.html`
- `/(.*)` → `api/index.py`

## Architecture

### Entry Points
- `api/index.py:26` - POST `/api/webhook` - Telegram message intake
- `api/index.py:33` - POST `/api/pulse` - Scheduled briefing engine

### Core Modules
- `core/webhook.py` - Telegram command handling, raw dump capture
- `core/pulse.py` - AI briefing generation, task management, calendar sync

### Database (Supabase)
- `tasks` - Task management
- `raw_dumps` - Telegram message queue
- `projects` - Project/org routing (SOLVSTRAT, PRODUCT_LABS, CRAYON, PERSONAL, CHURCH)
- `resources` - Saved links/library
- `missions` - Strategic goals
- `people` - Contacts
- `core_config` - Season context

### External Integrations
- Google Calendar API (event blocks)
- Google Tasks API (checklist)
- Gemini AI (`gemini-3-flash-preview`)
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
- Use `format_rfc3339()` in `core/pulse.py:35` to sanitize times
- Format: `YYYY-MM-DDTHH:MM:SS+05:30`

### Security
- Telegram webhook validates `TELEGRAM_CHAT_ID`
- Pulse endpoint validates `PULSE_SECRET` header
- Supabase uses `SUPABASE_SERVICE_ROLE_KEY` (bypasses RLS)

### Pulse Cron Schedule (UTC)
- Weekdays: `30 3,6,10,13 * * 1-5` (9AM, 12PM, 4PM, 7PM IST)
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
```

## Testing
- GitHub Actions: `workflow_dispatch` in `.github/workflows/pulse.yml` for manual trigger
- Local: Send POST to `/api/pulse` with header `x-pulse-secret`
