# Integrated-OS

A proprietary "Executive Command" system — an AI-powered Chief of Staff. It turns raw input (voice, text, images, email, Teams, WhatsApp) into tasks, syncs with Google Calendar/Tasks, and delivers judgment-timed briefings that exercise judgment about what matters now.

## 🏗️ Core Architecture

- **Runtime:** FastAPI (Python 3.11+) deployed on **Modal**. Supabase (Postgres) for storage, Upstash Redis for the rate limiter and the test-suite sandbox lock.
- **Intake:** A webhook receiver (`core/webhook/`) handling **Telegram, email, Teams, Outlook, and WhatsApp-via-Beeper** — with direction-awareness (your own sends never surface as inbound items).
- **Intelligence:** A Gemini processing layer (`core/llm/`) that classifies intent (Task / Note / Research / Noise), extracts entities, and renders briefings. `gemini-3.5-flash-lite` for classification, `gemini-3.6-flash` for synthesis.
- **The Pulse:** The scheduled brain (`core/pulse/`) — a briefing engine with an agent loop, calendar conflict guard, hybrid (vector + graph) memory recall, and Google two-way sync.
- **Multi-tenant:** Every tenant is isolated behind a tenant-aware client (`owner_id` scoping, RLS-granted roles) — see `product-summary/` for the M3 design.
- **Learning loop:** Every decision — approve/reject/snooze/correct — persists to the `decisions` ledger and trains subsystem patterns, so "Not now" never silently resets.
- **The app:** A Flutter client (`rhodey_app/`) with onboarding, personas, home-screen modes (proceed/decide/sprint/catch-up/wrap), an approval surface with per-item undo, and a Telegram-independent reply path.
- **The agents:** Specialized workers (`core/agents/`) — e.g., the research agent that browses the live web and returns a dossier into your staging area.

## 🧭 Where to go from here

| Need | Doc |
|---|---|
| Product vision & mindset (read first) | `product-summary/00-vision-and-mindset.md` |
| Verified reference | `product-summary/99-architecture-reference.md` |
| Database schema (59 tables, live-verified) | `product-summary/05-database-schema.md` |
| Test suite & gates (fast/nightly) | `tests/README.md`, `plans/75-comprehensive-test-plan.md` |
| Session history | `session-notes/` |

## 🚀 Quick-Start Setup

Populate a `.env` file with:

- **AI:** `GEMINI_API_KEY`
- **Database:** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- **Communication:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Google Auth:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- **Security:** `PULSE_SECRET` (authorizes cron jobs)

## 🧪 Testing

```bash
python3 scripts/run_tests.py --tier fast   # hermetic tiers: L0–L2-mock + app
python3 scripts/run_tests.py --tier nightly  # live tiers: L2-live–L4 + coverage + leak guard
```

The pre-push hook runs the fast tier automatically. Live layers serialize behind the Redis sandbox lock and self-heal via clean-slate pre-delete.
