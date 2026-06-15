# 23. Governance & Security

## Strategic Season Governance

### The Concept

A "Season Context" stored in `core_config` acts as the system's strategic north star. It sets 3-6 month priorities that influence how the AI schedules, prioritizes, and delivers briefings.

### Setting the Season

Via Telegram:
```
User: /season Q3 Focus: Debt recovery and Qhord GTM launch. 
       Build team capacity. [EXPIRY: 2026-09-30]
System: ✅ Season Updated. Target Locked.
```

### How It Works in Briefings

The season context is injected directly into the briefing prompt:

```
STRATEGIC CONTEXT: Q3 Focus: Debt recovery and Qhord GTM launch.
                  Build team capacity. [EXPIRY: 2026-09-30]
```

### Expiry Detection

If the season context includes an `[EXPIRY: YYYY-MM-DD]` tag, the engine checks it on every Pulse run:

```python
expiry_match = re.search(r'\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]', season_config)
if expiry_match:
    expiry_date = datetime.strptime(expiry_match.group(1), "%Y-%m-%d")
    if now > expiry_date:
        system_context = "CRITICAL: Season Context EXPIRED."
```

When expired, the briefing prompt starts with a CRITICAL header — ensuring the user knows they're operating on stale strategy.

## Org Routing Taxonomy

### The 5 Tags

Every piece of data in the system is routed through one of 5 org tags:

| Tag | Context | Domain |
|-----|---------|--------|
| `SOLVSTRAT` | work | Client services & delivery. Software development, consulting, client projects |
| `QHORD` | work | Product GTM & launch (June 2026). Product development, marketing, sales |
| `CRAYON` | work | Company umbrella. Governance, legal, tax, compliance, admin structure |
| `ASHRAYA` | personal | Church administration, operations, accounts, facility management |
| `PERSONAL` | personal | Family, home, health, personal admin, spiritual practices, journaling |

### How Routing Works

**Classification**: When Gemini classifies a message, it also assigns an entity tag:
```
PROJECT ROUTING: Route client tech work to SOLVSTRAT. Qhord product GTM to QHORD.
Ashraya church admin to ASHRAYA. Family/personal to PERSONAL.
CRAYON for corporate governance/tax/legal.
```

**Stealth enforcement**: The entity tag is in the JSON data, NEVER in the Telegram receipt.

**Briefing filtering**: Tasks are grouped by org_tag in briefings:
- `SOLVSTRAT`, `QHORD`, `CRAYON` → **🚀 Work** section
- `PERSONAL` → **🏠 Home** section (family, personal)
- `ASHRAYA` → **⛪ Church** section (dedicated section for Ashraya tasks)

**Practice exclusion**: Work entities are excluded from practice detection — only personal habits become practices.

## CI/CD & Autonomous Operations

### 7 GitHub Actions Workflows

| Workflow | Schedule | Timeout | What It Does |
|----------|----------|---------|-------------|
| `pulse.yml` | 5x weekday + 2x weekend | 20 min | Full intelligence cycle: archive ingest → graph backfill → pulse briefing |
| `quick_process.yml` | Every 5 min (7:30AM-10:30PM IST) | 5 min | Process pending raw_dumps → create tasks + notes |
| `email_ingest.yml` | 4x weekday + 2x weekend | 10 min | Gmail + Outlook fetch → classify → pending tasks + drafts |
| `research_worker.yml` | 2x daily | 10 min | Agent queue → Jina search → Gemini dossier |
| `synthesis.yml` | 1x daily (night) | 15 min | Brain synthesis: canonical page consolidation |
| `janitor.yml` | 4x daily | 5 min | Pipeline health, zombie recovery, failed queue retry |
| `cleanup.yml` | 1x weekly (Sunday) | 10 min | Orphan cleanup, stale dump purging |

### Schedule Strategy

The cron timing is aligned to IST (UTC+5:30):
- Weekday pulses: 5AM, 7:30AM, 11:30AM, 2:30PM, 5:30PM IST
- Weekend pulses: 8AM, 3PM IST
- Email ingest runs 30 minutes BEFORE each pulse (so fresh emails are included in the briefing)
- Quick process runs every 5 minutes during waking hours
- All workflows use GitHub Actions free tier — zero infrastructure cost

### Failure Notification

Every workflow has a `failure()` step that echoes to GitHub logs. Critical failures (pulse, email ingest) are also reported via Telegram.

## Security Model

### Authentication Layers

| Endpoint | Method | Mechanism |
|----------|--------|-----------|
| `/api/webhook` | POST | Chat ID authorization (TELEGRAM_CHAT_ID env var) |
| `/api/pulse` | POST | HMAC-SHA256 signature (X-Rhodey-Signature header) + PULSE_SECRET |
| `/api/*` (frontend) | Various | X-API-Key header with constant-time comparison |
| `/` (health) | GET | None (public) |
| Supabase | — | SUPABASE_SERVICE_ROLE_KEY (bypasses RLS) |

### HMAC Verification for Pulse

```python
def verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Frontend API Key Auth

```python
def require_api_auth(request: Request):
    api_key = request.headers.get("X-API-Key")
    expected = os.getenv("API_SECRET_KEY")
    if not expected:
        return  # No key configured → allow all (dev mode)
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
```

Constant-time comparison via `hmac.compare_digest` prevents timing attacks.

### Data Deletion Safety

A non-negotiable rule: NEVER delete database records without explicit user approval. All DELETE operations must use `--dry-run` first and present what would be affected before executing.

### Secrets Management

Required environment variables (stored in GitHub Secrets for CI, Vercel Environment Variables for deployment):
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
PULSE_SECRET, API_SECRET_KEY
GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
GOOGLE_SHEET_ID, OPENROUTER_API_KEY
```
