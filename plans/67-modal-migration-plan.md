# Part 67: Modal Migration Plan — Vercel → Modal for Python Backend

**Status: ✅ EXECUTED & LIVE (commit `26033ec`, Jul 2026)**

> This plan is complete. The FastAPI backend runs on Modal via `infra/modal_app.py`
> (`https://danielyashwant--rhodey-os-web-endpoint.modal.run`); `modal>=1.0.0` is pinned in
> requirements.txt. The root `vercel.json` / `.vercelignore` were removed (Aug 2026) and
> `validate_deployment.yml` now polls the Modal `/api/health` endpoint instead of Vercel.
> The Next.js frontend is a separate project (talks to Supabase directly) — see `frontend/`.
> The text below is retained as the historical record of the migration.

**Date:** July 26, 2026
**Target Latency Reduction:** ~12-21s (from ~30-45s down to ~15-22s)
**Cost Impact:** $7.12/mo (covered by Modal's $30/mo free credit) — **You pay ₹0**

## Executive Summary

This plan migrates the **Python FastAPI backend** (`api/index.py` + 72+ supporting files in `core/`) from **Vercel Serverless** to **Modal** while keeping the **Next.js frontend on Vercel** (unchanged).

### What moves
| Component | Current | Target |
|---|---|---|
| **FastAPI app** (all API endpoints) | Vercel (`api/index.py`) | Modal (`@modal.asgi_app`) |
| **Sentinel** (every 5 min) | External cron-job.org → Vercel API | Native Modal `Cron("*/5 * * * *")` |
| **Decision Pulse** (every 30 min) | External cron-job.org → Vercel API | Native Modal `Cron("*/30 * * * *")` |
| **Evening Roundup** (2x daily) | External cron-job.org → Vercel API | Native Modal `Cron("0 14,20 * * *")` |
| **Pulse Engine** (6x weekdays) | GitHub Actions → `core/pulse_cli.py` | Native Modal `Cron(...)` |
| **Health Check** (every 2h) | GitHub Actions → `scripts/run_health.py` | Native Modal `Cron(...)` |

### What stays on Vercel
- **Next.js frontend** (dashboard, graph UI, etc.) — Vercel Hobby, still $0
- **Frontend API routes** (Next.js `/api/` proxy routes) — unchanged

### What's eliminated
- **cron-job.org** (3 cron jobs → 0)
- **GitHub Actions** for scheduled Pulse (still used for archive_ingest, backfill_graph, notebooklm-sync)
- **PostgREST HTTP overhead** — replaced by asyncpg (direct SQL)
- **Vercel cold starts** — `min_containers=1` keeps 1 container always warm
- **55s safety net hack** — Modal has 150s timeout for web endpoints, hours for background
- **`asyncio.to_thread()` wrappers** — asyncpg is natively async

---

## Architecture After Migration

```
┌──────────────────────────────────────────────────────────────────┐
│                         Modal (Python Backend)                    │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │  FastAPI App          │  │  Background Workers              │  │
│  │  (min_containers=1)   │  │  (same warm container pool)     │  │
│  │                       │  │                                  │  │
│  │  /api/webhook         │  │  sentinel()   — Cron */5 * * * *│  │
│  │  /api/pulse           │  │  decision_pulse — Cron */30 * * *│  │
│  │  /api/send-message    │  │  pulse_engine — Cron weekdays   │  │
│  │  /api/tasks           │  │  health_check — Cron every 2h   │  │
│  │  /api/graph-*         │  │  roundup     — Cron 14,20 daily  │  │
│  │  ... (+30 endpoints)  │  │                                  │  │
│  └──────────┬───────────┘  └──────────────┬───────────────────┘  │
│             │                              │                       │
│             └──────────────┬───────────────┘                       │
│                            ▼                                       │
│              ┌────────────────────────┐                            │
│              │  asyncpg Pool          │  ◄── Direct SQL, no HTTP   │
│              │  (persistent conn)     │                            │
│              └───────────┬────────────┘                            │
└──────────────────────────┼────────────────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │    Supabase            │
              │    (Postgres DB)       │
              └────────────────────────┘

    Vercel (frontend only)
    ┌─────────────────────────────┐
    │  Next.js Dashboard          │
    │  (unchanged)                │
    │  Fetches from Modal URL     │
    └─────────────────────────────┘
```

---

## Migration Phases

### Phase 0: Prerequisites (1 hour)

**Step 0.1: Install Modal SDK**
```bash
pip install modal
modal setup  # Authenticates with GitHub/Google
```

**Step 0.2: Create Modal secrets**
```bash
modal secret create rhodey-os \
  SUPABASE_URL="..." \
  SUPABASE_SERVICE_ROLE_KEY="..." \
  GEMINI_API_KEY="..." \
  GEMINI_API_KEY_2="..." \
  GEMINI_API_KEY_3="..." \
  TELEGRAM_BOT_TOKEN="..." \
  TELEGRAM_CHAT_ID="..." \
  PULSE_SECRET="..." \
  API_SECRET_KEY="..." \
  GOOGLE_REFRESH_TOKEN="..." \
  GOOGLE_CLIENT_ID="..." \
  GOOGLE_CLIENT_SECRET="..." \
  UPSTASH_REDIS_URL="..." \
  UPSTASH_REDIS_TOKEN="..." \
  OUTLOOK_CLIENT_ID="..." \
  OUTLOOK_CLIENT_SECRET="..." \
  OUTLOOK_REFRESH_TOKEN="..." \
  OUTLOOK_TENANT_ID="..." \
  OUTLOOK_SCOPES="..." \
  FIREBASE_SERVICE_ACCOUNT="..." \
  FIREBASE_PROJECT_ID="..." \
  WHATSAPP_INGEST_SECRET="..." \
  OPENROUTER_API_KEY="..." \
  OPENROUTER_BASE_URL="..." \
  JINA_API_KEY="..." \
  GMAIL_SENDER_EMAIL="..." \
  GOOGLE_SHEET_ID="..." \
  GOOGLE_DRIVE_CALLS_FOLDER_ID="..." \
  WHISPER_MODEL_SIZE="..." \
  WEBHOOK_BASE_URL="..." \
  DRIVE_WATCH_CHANNEL_ID="..." \
  GITHUB_TOKEN="..." \
  GITHUB_OWNER="Crayon-Biz-LLP" \
  GITHUB_REPO="integrated-os" \
  RETRIEVAL_ASSOCIATIVE_ENABLED="true" \
  RETRIEVAL_INDEXING_ENABLED="true"
```

**All ~40 env vars in one command.** This replaces Vercel's Environment Variables dashboard for the backend.

---

### Phase 1: Create Modal App Entry Point (2-3 hours)

**New file: `infra/modal_app.py`**

This is the main Modal entry point. It replaces `api/index.py` as the deployment target for Modal.

```python
import modal
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Modal App Definition ──
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    .apt_install("ffmpeg")  # for whisper if needed
)

app = modal.App("rhodey-os", image=image)
secrets = [modal.Secret.from_name("rhodey-os")]

# ── FastAPI Instance ──
fastapi_app = FastAPI(title="Integrated-OS (Modal)")

# CORS (same as current vercel.json allows all)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register all routes from api/index.py ──
# This is imported from the existing api/index.py
# but wrapped with Modal's ASGI app decorator

@app.function(
    secrets=secrets,
    min_containers=1,          # ← Keeps 1 container warm 24/7
    container_idle_timeout=300, # If no requests for 5 min, scale to 0
    allow_concurrent_inputs=10, # Handle 10 concurrent requests
)
@modal.asgi_app()
def web_endpoint():
    """The FastAPI app — serves all API endpoints."""
    from api.index import app as fastapi_app
    return fastapi_app


# ── Background Workers ──
# These run on the SAME warm container as the web endpoint
# because they use @app.function with the same app.

@app.function(
    secrets=secrets,
    schedule=modal.Cron("*/5 * * * *"),  # Every 5 min
    timeout=120,                          # 2 min max (was 30s on Vercel)
)
async def sentinel():
    """Replace cron-job.org → /api/sentinel"""
    from core.pulse.sentinel import process_sentinel
    result = await process_sentinel(
        auth_secret=os.environ["PULSE_SECRET"],
        trigger="modal_cron"
    )
    return result


@app.function(
    secrets=secrets,
    schedule=modal.Cron("*/30 * * * *"),  # Every 30 min
    timeout=120,
)
async def decision_pulse():
    """Replace cron-job.org → /api/decision-pulse"""
    from core.pulse.decision_pulse import process_decision_pulse
    result = await process_decision_pulse(
        auth_secret=os.environ["PULSE_SECRET"],
        trigger="modal_cron"
    )
    return result


@app.function(
    secrets=secrets,
    schedule=modal.Cron("0 2,6,9,12 * * 1-5"),  # Weekdays IST
    timeout=300,
)
async def pulse_engine_weekday():
    """Replace GitHub Actions Pulse workflow"""
    from core.pulse_cli import main as run_pulse
    await run_pulse()


@app.function(
    secrets=secrets,
    schedule=modal.Cron("0 2,9 * * 0,6"),  # Weekends IST
    timeout=300,
)
async def pulse_engine_weekend():
    await run_pulse()


@app.function(
    secrets=secrets,
    schedule=modal.Cron("0 */2 * * 1-5"),  # Weekdays every 2h
    timeout=60,
)
async def health_check():
    """Replace GitHub Actions Health workflow"""
    from core.pulse.pipeline import run_full_health_check
    result = await run_full_health_check()
    return result


@app.function(
    secrets=secrets,
    schedule=modal.Cron("0 14,20 * * *"),  # For Asia/Kolkata timezone
    timeout=60,
)
async def roundup():
    """Replace cron-job.org → /api/roundup"""
    from api.index import roundup_logic
    await roundup_logic()


# ── Optional: GPU-powered Whisper transcription ──
@app.function(
    secrets=secrets,
    gpu="T4",
    timeout=600,
    volumes={
        "/models": modal.Volume("whisper-models"),
    },
)
async def transcribe_call(audio_path: str):
    """Transcribe call recordings locally (free GPU credits)."""
    import whisper
    model = whisper.load_model("large-v3", download_root="/models")
    result = model.transcribe(audio_path)
    return result["text"]
```

**Files to create/modify:**
| File | Action |
|---|---|
| `infra/modal_app.py` | **NEW** — Main Modal entry point |
| `infra/__init__.py` | **NEW** — Empty init |

---

### Phase 2: asyncpg Migration (3-5 days)

**This is the critical change.** The current `core/services/db.py` uses the Supabase Python client (PostgREST HTTP). We add asyncpg support alongside it.

**New file: `core/services/async_db.py`**

```python
"""Async PostgreSQL connection pool for Modal.

Replaces PostgREST HTTP calls (300-500ms each) with direct SQL (5-15ms each).
Uses statement_cache_size=0 to work with Supabase's PgBouncer.

Usage:
    from core.services.async_db import get_pool, sql
    
    async with get_pool() as conn:
        rows = await conn.fetch("SELECT * FROM tasks WHERE id = $1", task_id)
"""

import os
import asyncpg
from contextlib import asynccontextmanager

_pool: asyncpg.Pool | None = None

async def init_pool():
    """Initialize global asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return
    
    dsn = os.getenv("SUPABASE_URL")
    if not dsn:
        raise ValueError("SUPABASE_URL not set")
    
    # Supabase uses PgBouncer — prepared statements must be disabled
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        user="postgres",
        password=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        min_size=1,
        max_size=5,
        statement_cache_size=0,  # Required for PgBouncer compatibility
        command_timeout=30,
    )

@asynccontextmanager
async def get_pool():
    """Get the global connection pool."""
    if _pool is None:
        await init_pool()
    try:
        yield _pool
    except Exception:
        if _pool:
            await _pool.close()
            _pool = None
        raise

# ── SQL helper functions ──

def sql(query: str, *args):
    """Placeholder — actual query execution needs a connection from pool."""
    pass

async def fetch(query: str, *args):
    """Fetch rows from the database."""
    async with get_pool() as pool:
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

async def fetchrow(query: str, *args):
    """Fetch a single row."""
    async with get_pool() as pool:
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

async def execute(query: str, *args):
    """Execute a query (INSERT/UPDATE/DELETE without returning rows)."""
    async with get_pool() as pool:
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)
```

**Migration strategy:** Sequential replacement — do NOT rewrite 72 files at once.

1. First, migrate the **handler pre-work** (15-20 calls in `handler.py`, `dispatch.py`):
   - These are the most latency-sensitive (block the response pipeline)
   - Replace `supabase.table('x').select(...).eq(...).execute()` patterns with `async_db.fetch()`

2. Second, migrate the **associative retrieval** pipeline (in `core/retrieval/`):
   - These are the most expensive (5-8s of HTTP round-trips)
   - Replace with stored SQL functions or direct asyncpg queries

3. Leave everything else on the Supabase client:
   - The `get_supabase()` function still works for non-critical paths
   - This is a hybrid approach — only hot paths use asyncpg

**Migration rules:**
- Every `.rpc('name', {...})` call → `SELECT * FROM name(...)` via asyncpg
- Every `.table('x').select('y, z').eq('a', 'b').execute()` → `SELECT y, z FROM x WHERE a = $1`
- Every `.upsert(data, on_conflict='key')` → `INSERT INTO x (...) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET ...`
- Every `.ilike('col', '%val%')` → `WHERE col ILIKE '%' || $1 || '%'`
- Every `.in_('col', [1,2,3])` → `WHERE col = ANY($1::int[])`

**Critical: Nested JSON expansion.** PostgREST auto-expands `organizations(name)` into nested dicts. asyncpg returns flat rows. Every code path that reads `p['organizations']['name']` must be updated to a JOIN.

```python
# Before (PostgREST auto-expansion):
proj = supabase.table('projects').select('name, organizations(name)').eq('id', 1).execute()
org_name = proj.data['organizations']['name']

# After (asyncpg JOIN):
row = await async_db.fetchrow("""
    SELECT p.name, o.name as org_name 
    FROM projects p 
    LEFT JOIN organizations o ON o.id = p.organization_id 
    WHERE p.id = $1
""", 1)
org_name = row['org_name']
```

---

### Phase 3: Update api/index.py for Modal Context (1 hour)

The API routes themselves don't change — the same FastAPI app runs on both platforms. But we need to:

1. Remove the 55s `asyncio.wait_for` (Modal has 150s timeout)
2. Remove the thread pool upgrade hack (asyncpg is natively async)
3. Keep the Supabase client for non-hot paths (hybrid mode)

```python
# api/index.py — changes needed

# BEFORE (Vercel-specific):
app.on_event("startup")
async def _upgrade_thread_pool():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=16))

# AFTER (remove — asyncpg doesn't need thread wrappers):
# No thread pool upgrade needed. asyncpg is truly async.

# BEFORE (Vercel 60s timeout hack):
async def webhook_route(request: Request):
    try:
        await asyncio.wait_for(process_webhook(update), timeout=55)
    except asyncio.TimeoutError:
        print("Webhook processing timed out (>55s).")

# AFTER (Modal — 150s timeout, no need for safety net):
async def webhook_route(request: Request):
    await process_webhook(update)
    return {"success": True}
```

---

### Phase 4: Background Workers — Modal Natives (1 day)

**What changes:**

| Current | Modal | File |
|---|---|---|
| cron-job.org → `/api/sentinel` (every 5 min) | `@app.function(schedule=Cron("*/5 * * * *"))` | `infra/modal_app.py` |
| cron-job.org → `/api/decision-pulse` (every 30 min) | `@app.function(schedule=Cron("*/30 * * * *"))` | `infra/modal_app.py` |
| cron-job.org → `/api/roundup` (2x daily) | `@app.function(schedule=Cron("0 14,20 * * *"))` | `infra/modal_app.py` |
| GitHub Actions → `core/pulse_cli` (6x weekday) | `@app.function(schedule=Cron(...))` | `infra/modal_app.py` |
| GitHub Actions → `scripts/run_health` (every 2h) | `@app.function(schedule=Cron(...))` | `infra/modal_app.py` |

**Note:** The background workers run on the **same warm container** as the FastAPI app. They DON'T need separate containers. The `min_containers=1` setting keeps everything warm.

**What stays in GitHub Actions:**
- `pulse.yml` Step 1 (`archive_ingest`) — runs as GHA because it's long batch processing
- `pulse.yml` Step 2 (`backfill_graph`) — same reason
- `notebooklm-sync.yml` — syncs docs on push, independent of backend

---

### Phase 5: Update Frontend to Talk to Modal (30 min)

The Next.js frontend currently fetches from Vercel's domain. It needs to fetch from Modal's domain instead.

**Changes in `frontend/`:**

```typescript
// lib/config.ts — make API URL configurable
const API_URL = process.env.NEXT_PUBLIC_API_URL || 
  (process.env.NODE_ENV === 'development' 
    ? 'http://localhost:8000' 
    : 'https://rhodey-os.modal.run');  // ← Modal's deployed URL
```

Add `NEXT_PUBLIC_API_URL` to Vercel's frontend env vars pointing to Modal's URL.

---

### Phase 6: Deployment Pipeline (1 day)

**New file: `.github/workflows/deploy-modal.yml`**

```yaml
name: Deploy to Modal
on:
  push:
    branches: [main]
    paths:
      - 'api/**'
      - 'core/**'
      - 'infra/**'
      - 'requirements.txt'

jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install Modal
        run: pip install modal
      - name: Deploy
        env:
          MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
          MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
        run: modal deploy infra/modal_app.py
```

**Note:** Modal requires a `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` for CI/CD deployment (not the same as `modal setup` credentials). Generate these from the Modal dashboard → Tokens.

**After deployment:** The API is available at `https://rhodey-os.modal.run`.

---

### Phase 7: Update External Services (30 min)

After Modal deployment:

1. **Telegram webhook URL**: Change from `https://integrated-os.vercel.app/api/webhook` → `https://rhodey-os.modal.run/api/webhook`
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://rhodey-os.modal.run/api/webhook"
   ```

2. **WhatsApp ingest URL**: Update MacroDroid webhook target to Modal URL.

3. **Vercel env vars**: Remove all backend-only env vars from Vercel (keep frontend vars like `NEXT_PUBLIC_API_URL`).

4. **cron-job.org**: Delete all 3 cron jobs (sentinel, decision-pulse, roundup).

5. **GitHub Actions**: Keep `pulse.yml` (Steps 1-2 for archive/backfill) but skip Step 3 (the Pulse CLI). Keep `notebooklm-sync.yml`. Can remove `health.yml` (replaced by Modal cron).

---

## Effort Summary

| Phase | What | Files Changed | Effort | Risk |
|---|---|---|---|---|
| 0 | Prerequisites | 0 (CLI setup) | 1 hour | None |
| 1 | Modal app entry | 2 new files | 2-3 hours | Low |
| 2 | asyncpg migration | ~15-20 files | 3-5 days | Medium |
| 3 | API cleanup | 1 file | 1 hour | Low |
| 4 | Background workers | 1 file | 1 day | Low |
| 5 | Frontend config | 1 file | 30 min | Low |
| 6 | Deployment pipeline | 1 new file | 1 day | Low |
| 7 | External services | 0 files (UI changes) | 30 min | Low |
| **Total** | | **~20-25 files** | **~5-8 days** | **Medium** |

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| **asyncpg + PgBouncer** — Prepared statements crash | Set `statement_cache_size=0` — **non-negotiable** |
| **Nested JSON expansion** — PostgREST auto-unfolds `orgs(name)` | Manually rewrite to JOINs. Audit each call site. |
| **Deployment downtime** — `modal deploy` restarts container | Use `modal deploy --detach` or blue-green (Modal Pro) |
| **Secrets drift** — Vercel vs Modal secrets out of sync | Single source of truth in `modal secret create` command |
| **Frontend breaks** if Modal URL changes | Set `NEXT_PUBLIC_API_URL` in Vercel frontend env vars |
| **Cold start after code deploy** | Every `modal deploy` causes ~1-2s restart. Acceptable. |

---

## Timeline & Rollout Sequence

```
Week 1:  Phase 0-1 (Setup + Modal app + basic deploy)
         Day 1:   Install Modal, create secrets
         Day 2:   Write infra/modal_app.py
         Day 3:   Deploy to Modal, verify health endpoint works
         
Week 2:  Phase 2 (asyncpg migration — hot paths)
         Day 4-5: Migrate handler.py + dispatch.py DB calls
         Day 6:   Migrate retrieval pipeline DB calls
         Day 7:   Test hybrid mode (asyncpg for hot path, Supabase client for rest)

Week 3:  Phase 3-5 (Background workers + frontend + deploy)
         Day 8-9:  Set up Modal cron schedules
         Day 10:   Update frontend + deployment pipeline
         Day 11:   Update external services (Telegram webhook)
         Day 12:   Monitor, fix edge cases
```

---

## What This Enables After Migration

| Capability | Before (Vercel) | After (Modal) |
|---|---|---|
| **Request timeout** | 60s hard limit | **150s** for web, **hours** for background |
| **Cold starts** | ~1-3s after idle | **Zero** with `min_containers=1` |
| **DB calls** | PostgREST HTTP (~200-500ms each) | **asyncpg (~5-15ms each)** |
| **Background tasks** | Killed after response, enrichment queue needed | `.spawn()` survives, **no enrichment queue hacks** |
| **GPU/Whisper** | Impossible | **Free GPU credits** for local transcription |
| **Scheduling** | External cron-job.org + GitHub Actions | **Native Modal Cron** — no external dependencies |
| **Stateful caches** | Lost on cold start | **Persistent in RAM** across calls |
| **Monthly cost** | $0 (Vercel Hobby, but at ~$20/mo if upgraded) | **$0** ($30 Modal credits cover $7.12 usage) |
