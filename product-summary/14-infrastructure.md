# Infrastructure & Deployment

## Runtime: Modal (replaces Vercel)

The FastAPI backend runs on **Modal** — a serverless platform for Python with zero cold starts, 300s timeout, and concurrent input handling.

**Deployment:**
```bash
modal deploy infra/modal_app.py
```

**Key configuration** (`infra/modal_app.py`):
- `min_containers=1` — zero cold starts
- `timeout=300` — 5-minute function timeout (vs Vercel's 60s)
- `@modal.concurrent(max_inputs=10)` — handles 10 concurrent requests per container
- `Volumes` — persistent storage for retrieval cache
- `Image` — Python 3.11 with `pip install` from `requirements.txt`
- `Secrets` — all env vars in Modal secret `rhodey-os`

**Web endpoint:** `https://danielyashwant--rhodey-os-web-endpoint.modal.run`

**Modal secret** (`rhodey-os`): Created via `scripts/create_modal_secret.py` from `.env` file. Contains all environment variables (Supabase, Gemini, Google, Telegram, etc.).

## Historical Note

Before Modal (Jul 26, 2026), the backend ran on **Vercel** as serverless functions (`api/index.py`). Vercel's 60s timeout and cold kills of `asyncio.create_task()` were the primary motivation for migrating. Vercel deployment is now idle — all traffic routes to Modal.

## FastAPI Entry Point

Both Modal and legacy Vercel deployments wrap the same FastAPI app from `api/index.py`. The entry point is determined by the runtime:

- **Modal:** `infra/modal_app.py` wraps `api.index.app` with Modal's `@app.function()` decorator
- **Vercel (inactive):** `api/index.py` uses FastAPI's `app` directly, deployed via vercel.json

## Database: Supabase (Postgres + pgvector)

- **Host:** Supabase project (shared PostgreSQL with pgvector extension)
- **Auth:** Service role key (bypasses RLS)
- **Connection:** PostgREST REST API via `supabase-py` client
- **Additional connection:** asyncpg pool for hot-path reads (Phase 2 — see `core/services/async_db.py`)
- **Pooler:** Supabase connection pooler (configured via `SUPABASE_POOLER_HOST`)

## Scheduled Jobs

### External: cron-job.org (high-frequency)
| Job | Endpoint | Schedule | Purpose |
|-----|----------|----------|---------|
| Sentinel Nudge | POST /api/sentinel | Every 5 min | Meeting alarms + piggyback jobs |
| Decision Pulse | POST /api/decision-pulse | Every 30 min | Pending approvals check |
| Evening Roundup | POST /api/roundup | 2PM, 8PM IST | Evening check-in |

**Auth:** All endpoints validate `x-pulse-secret` header matching `PULSE_SECRET` env var.

### GitHub Actions (push + scheduled)
| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `health.yml` | Every 2h weekdays | Consolidated health check (DLQ, errors, LLM degradation) |
| `notebooklm-sync.yml` | On push to main | Sync Google Docs → Notebook LM |
| `email_ingest.yml` | Scheduled | Gmail + Outlook inbox polling |
| `brain_synth_v2.yml` | Weekly | Canonical page synthesis |

**Note:** Main briefing and Pulse Engine are now triggered via cron-job.org pointing to Modal endpoints, not GitHub Actions.

## Key Environment Variables

```
SUPABASE_URL                   # Supabase project URL
SUPABASE_SERVICE_ROLE_KEY      # Service role key (bypasses RLS)
SUPABASE_DB_PASSWORD           # Postgres password for asyncpg pool
SUPABASE_POOLER_HOST           # Pooler hostname (aws-{region}.pooler.supabase.com)
GEMINI_API_KEY                 # Primary Gemini API key
GEMINI_API_KEY_2               # Secondary key (failover)
GEMINI_API_KEY_3               # Tertiary key (failover)
TELEGRAM_BOT_TOKEN             # Rhodey bot token
TELEGRAM_CHAT_ID               # Danny's chat ID
PULSE_SECRET                   # Shared secret for cron endpoints
API_SECRET_KEY                 # Frontend API auth header
WHATSAPP_INGEST_SECRET         # WhatsApp webhook auth
GOOGLE_REFRESH_TOKEN           # Google OAuth refresh token
GOOGLE_CLIENT_ID               # Google OAuth client ID
GOOGLE_CLIENT_SECRET           # Google OAuth client secret
GOOGLE_SHEET_ID                # Google Sheets ID for archive ingest
OPENROUTER_API_KEY             # Fallback LLM provider
UPSTASH_REDIS_REST_URL         # Redis cache URL
UPSTASH_REDIS_REST_TOKEN       # Redis cache token
```

## Retrieval Cache (Redis)

Upstash Redis is used for:
- LLM entity extraction results (1h TTL)
- Embedding vectors (24h TTL)
- Cache keys: SHA-256 hashes

Cache is fail-open — if Redis is unavailable, all operations fall back to direct DB queries.

## Health Monitoring

Single health endpoint: `/api/health` (runs `run_full_health_check()` from `core/pulse/pipeline.py`)

Checks:
1. DLQ count — unresolved dead letter queue items
2. Error rate — recent system_audit_logs errors
3. LLM degradation — API failure rate in last hour

Triggered by `scripts/run_health.py` CLI, called by GitHub Actions `health.yml` every 2h on weekdays.

## Architecture Diagram (Text)

```
[Danny: Telegram / Web UI / Flutter App]
       │
       ▼
[Modal] ←── infra/modal_app.py
  │
  ├── Webhook: POST /api/webhook → handler.py
  ├── Pulse:   POST /api/pulse → briefing.py
  ├── Health:  GET  /api/health → pipeline.py
  └── Web UI:  GET  /api/* → FastAPI routes

[Cron-job.org] ─── POST /api/sentinel (every 5min)
[Cron-job.org] ─── POST /api/decision-pulse (every 30min)
[Cron-job.org] ─── POST /api/roundup (daily)

[GitHub Actions] ─── health.yml (2h), email_ingest.yml, notebooklm-sync.yml

[Supabase] ←── PostgREST + asyncpg pool
[Redis]    ←── Upstash REST API
[Gemini]   ←── Google AI API (multi-key failover)
[Google]   ←── OAuth2: Calendar, Tasks, Gmail, Docs, Drive
```

## File Structure

```
infra/
├── __init__.py
├── modal_app.py        # Modal deployment entry point
├── docs/               # Infrastructure documentation
api/
├── index.py            # FastAPI app (shared by Modal and legacy Vercel)
├── app.py              # OpenAPI metadata
├── briefing.py         # Briefing API routes
scripts/
├── create_modal_secret.py    # Modal secret creation from .env
├── run_health.py              # CLI health check
├── record_app_version.py      # CI app version tracking
```
