# Integrated-OS Agent Guide

## Project Overview
FastAPI-based executive command system deployed as Vercel serverless functions (Python 3.11, matches CI). Processes Telegram messages into tasks, syncs with Google Calendar/Tasks, sends AI-generated briefings via Telegram.

## Session Anchored Summary (Jun 23, 2026)

### Progress Done This Session
- **Comprehensive 38-Point Hardening (Tiers 0-5)**: Executed a massive codebase hardening pass to address 6 tiers of vulnerabilities.
- **Active Crashes & Secrets (Tier 0)**: Rotated and redacted hardcoded `config.json` and `frontend/.env.local` keys. Added `processing_completion` statuses to `raw_dumps_status_check`. Fixed context polarity (`.eq('is_current', True)`) and salience bugs.
- **Data Corruption (Tier 1)**: Restored entity extraction loop in `quick_process.py`. Fixed string formatting crashes in retrieval. Fixed `auto_approve` metadata overwrites. Stripped all app-level temporal versioning from `calendar.py` and Python codebase to enforce pure DB-trigger lineage.
- **Ghost Record Isolation (Tier 2)**: Added strict `.eq('is_current', True)` to 10 queries across Python and Next.js layers to prevent duplicate blocking and context pollution from archived rows.
- **Tests & Deploy (Tier 3)**: Pinned `requirements.txt`. Purged orphan `__pycache__` folders. Dropped stale RPCs. Created SQL migration for DB triggers. Fixed `test_retrieval.py` patches.
- **Security (Tier 4)**: Plugged 12 endpoint exception leaks. Hardened cron endpoint auth. Added `X-Goog-Channel-Token` validation to Google Drive webhook. Added frontend Dashboard auth guards.
- **Frontend (Tier 5)**: Fixed React 19 NeuralDisc refs read during render. Fixed Radix UI duplicate key selections. Fixed FullGraph simulation teardown issues.

### Key Decisions This Session
- **Strict Configuration Segregation**: The local AI (`opencode.json`) tokens for Vercel/Supabase are entirely separated from the deployed backend environment variables (`SUPABASE_SERVICE_ROLE_KEY`).
- **Complete reliance on PostgreSQL Triggers**: The Python layer no longer touches `is_current` or `supersedes_id` during updates — all temporal versioning is managed strictly by `BEFORE UPDATE` database triggers.

### Pending / Next Steps
- **Future graph improvements**: PIXI object pooling, smooth zoom/pan animations, multi-select + expand-in-place nodes, episode stream infinite scroll + date range
- **Decisions Table (TF-001)**: Structured `decisions` table to track explicit choices with lifecycle (active/superseded/reversed). Currently decisions are implicit in tasks/briefings.
- **Graph Edge Expiry (TF-002)**: `last_confirmed_at`/`valid_until` columns on `graph_edges` to prevent stale relationship poisoning.
- **People Table Enrichment (TF-003)**: Populate `org`, `last_interaction_date`, `notes` columns from graph edges.
- **Collaborator view**: Would require RLS policies and user-permission scoping (out of scope for current MVP)

## Key Commands

### Local Development
```bash
pip install -r requirements.txt
pip install uvicorn  # Not in requirements.txt
uvicorn api.index:app --reload --port 8000
```

### Pulse CLI (Local)
```bash
python core/pulse_cli.py         # Main AI briefing
python core/pulse_cli.py decisions  # Decision pulse (no AI)
# Both require PULSE_SECRET, Supabase, Gemini, Telegram vars
```

### Deployment
Vercel auto-deploys `main` branch. All routes rewritten to `api/index.py` (see `vercel.json`). Serverless function timeout: 60s.

## Architecture

### Entry Points
- `api/index.py:29` - POST `/api/webhook` - Telegram message intake
- `api/index.py:44` - POST `/api/pulse` - Scheduled briefing engine
- `api/index.py:318` - POST `/api/whatsapp-ingest` - WhatsApp notification ingest (MacroDroid)
- `core/pulse_cli.py` - CLI entry for pulse (used in CI)

### Core Modules
- `core/webhook/dispatch.py` - `interrogate_brain()` — universal query engine, anaphora resolution, source selection heuristics, proactive signal checks, time-aware calendar formatting
- `core/webhook/handler.py` - Telegram command handling, raw dump capture, message classification (Inline Keyboards replacing legacy shortcodes)
- `core/webhook/classify.py` - LLM-based intent classifier. Schedule questions with date ranges ("meetings this week?") route to QUERY, not DAILY_BRIEF. DAILY_BRIEF reserved for explicit daily overview requests ("good morning", "what's my day look like?").
- `core/pulse/engine.py` - AI briefing generation via `run_agent_loop` ToolRegistry, task management, calendar sync, and **Decision Pulse** (no AI, inline keyboard approvals).
- `core/pulse/context.py` - **Phase 2 Context Hydration Engine**. Uses TTL caches (`SimpleCache`) and hybrid vector+graph cross-referencing.
- `core/pulse/memory.py` - **Phase 3 Memory Engine**. Handles semantic retrieval with temporal decay and importance weighting (`match_memories_hybrid` — legacy, replaced by associative_retrieve).
- `core/pulse/entity_extractor.py` - Real-time Flash Lite entity extraction during webhook ingestion. Routes organizations and all LLM-extracted edges to pending tables (Step 1.5).
- `core/clarifier.py` - **Clarifier Phase 2 (LIVE)**. 6-function interface (`evaluate_node`, `evaluate_edge`, `build_batch`, `handle_response`, `next_shortcode`, `dedupe_batch`). Generates Telegram disambiguation questions for 85%+ similarity matches, auto-merge confirmations at 95%+, edge contradiction detection, low-confidence (<0.7) edge verification, and concept alias dedup.
- `core/agents/research_agent.py` - Research and embedding tasks
- `core/skills/` - Ingest (email, archive), nightly canonical brain synthesis, and graph sync scripts (run via CI)
- `core/retrieval/search.py` - `associative_retrieve()` — 7-signal ranking pipeline (semantic, PPR, recency, importance, project, specificity, person_boost). Redis-cached LLM extraction + embeddings, parallel DB queries via PostgREST nested joins, `asyncio.to_thread()` for sync ops.
- `core/retrieval/pipeline.py` - `index_memory()` / `schedule_index_memory()` — forward indexing: chunk → embed → extract → phrase node upsert → link passages → bundle. Module-level `Semaphore(3)` for extraction concurrency.
- `core/retrieval/graph.py` - Phrase node graph operations: `upsert_phrase_node()` (embeds new nodes at creation), `get_subgraph_edges()`, `update_node_stats()`.
- `core/retrieval/extractor.py` - `extract_triples()` — LLM entity extraction for phrase nodes from memory text.
- `core/retrieval/ranking.py` - `rank_memories()` — blends 7 signals with configurable weight constants.
- `core/retrieval/ppr.py` - `personalized_pagerank()` — 20 iterations, ~50ms on bounded subgraph (<2000 nodes).
- `core/retrieval/backfill.py` - Historical retrieval index backfill with checkpoint/resume via `retrieval_index_runs`.
- `core/retrieval/eval.py` - `run_eval()` — side-by-side comparison of legacy pgvector vs associative retrieval.
- `core/retrieval/config.py` - 4 per-site feature flags (`RETRIEVAL_ASSOCIATIVE_ENTITY_SUMMARY`, `RECENT_MEMORIES`, `HINDSIGHT`, `HYDRATE`) + `RETRIEVAL_INDEXING_ENABLED` + `RETRIEVAL_SHADOW_MODE`. Default: all OFF.
- `core/llm/embedding.py` - `get_embedding()` — multi-key failover across 3 Gemini API keys (iterates `get_gemini_clients()` on 429).
- `core/lib/redis_cache.py` - `cache_get()` / `cache_set()` — synchronous Redis helpers wrapped in `asyncio.to_thread()`.

### Database (Supabase)
- Uses `SUPABASE_SERVICE_ROLE_KEY` (bypasses RLS)
- Tables: `tasks`, `raw_dumps`, `memories`, `graph_nodes`, `graph_edges`, `projects`, `resources`, `clusters`, `people`, `core_config`, `messages`, `pending_graph_nodes`, `pending_graph_edges`
- **Note**: `raw_dumps` does NOT store embeddings - only `memories` table has embeddings
- **Note**: `pending_graph_nodes` holds new person/project nodes awaiting approval via Decision Pulse (`g{id}` shortcode). Also holds organization nodes and `status='flagged'` for ungrounded people awaiting clarification loop questions (`c{id}` shortcode).
- **Note**: `pending_graph_edges` holds all extracted edges awaiting approve/edit/reject via Decisions UI or Telegram (`pe{id}` shortcode). Since Step 1.5, entity_extractor.py routes ALL LLM-extracted edges here instead of direct graph_edges inserts.
- `messages` holds WhatsApp chats, Emails, and Call extracts with classification + approval status
- **Retrieval tables**: `retrieval_passages`, `retrieval_phrase_nodes` (with GIN trigram index), `retrieval_node_stats`, `retrieval_passage_phrase_links`, `retrieval_memory_bundle_links`, `retrieval_alias_edges` (3760 heuristic synonym bridges), `retrieval_index_runs` (checkpoint/resume).
- `backfill_graph.py` syncs graph edges from memories (has LLM fallback: Gemini → Gemma → OpenRouter). Excludes `raw_dumps` from extraction. Uses strict 5-node-type / 16-edge-type ontology with entity grounding.
- **Graph integrity**: Five layers — (1) Guard A deletes stale project edges before inserting new ones; (2) Guard B rejects hallucinated nodes via text-anchoring validation (no AUTHORED exception); (3) Guard C (HITL) gates ALL edges through `pending_graph_edges` + high-risk nodes through `pending_graph_nodes`; (4) Guard D dedup prevents label-drift re-insertion; (5) `concept` nodes supported via **Concept Fluidity (Synaptic Plasticity)** upgrade — extracted via `concept_sweep_batch.py`, deduped via 85%+ similarity check with 1-click merge, protected by HITL approval. No concept auto-creation. **Phase 1 Guards**: Guard 2 (`is_real_project`) hard-rejects ungrounded projects, Guard 3 (`has_structural_anchor`) flags ungrounded people/orgs.

### External Integrations
- **Gemini AI**: Briefing (`gemini-3.5-flash`), Classification (`gemini-3.1-flash-lite`), Embeddings (`gemini-embedding-2-preview`)
- **Native Control Layer**: Built-in to `core/pulse/llm.py` to enforce Pydantic JSON validation, targeted prompt mutations, and jittered exponential backoffs.
- **LLM Timeout Config**: `core/llm/config.py` defines `WorkloadProfile` profiles: INTERACTIVE (55s), SYNTHESIS (300s), BATCH (300s), EMBEDDING (120s). Heavy SYNTHESIS workloads (300s) are offloaded from Vercel to GitHub Actions jobs — Vercel only handles lightweight trigger webhooks, so the 60s serverless limit is not a constraint.
- Google Calendar API (event blocks), Google Tasks API (checklist)
- Telegram Bot API
- WhatsApp via MacroDroid (Android notification → webhook to `/api/whatsapp-ingest`)

### Shortcode Prefixes
| Prefix | Table | Action |
|--------|-------|--------|
| `e{id}` | `messages (email)` | Approve/reject email-suggested task |
| `c{id}` | `messages (call)` | Approve/reject call-extracted item |
| `w{id}` | `messages (whatsapp)` | Approve/reject WhatsApp-suggested task |
| `t{id}` | `messages (teams)` | Approve/reject Teams-suggested task |
| `g{id}` | `pending_graph_nodes` | Approve/reject new person/project node. Supports **NLP corrections** (e.g. "g2 is an organization, not a person"). Duplicate-re-insertion prevented by all-statuses cache, ILIKE dedup guard, and unique index on normalised label. |
| `pe{id}` | `pending_graph_edges` | Approve/reject pending graph edge. Supports new 16 edge types. |
| `c{id}` | `pending_graph_nodes` / `pending_graph_edges` (clarification_feedback) | Clarification loop question — reply with answer or context (e.g. "c3 Reginald Paulson — client from Equisoft"). |
| `{id}` (bare) | Tries email → call → whatsapp → graph → practice | Fallback compat |

## Project Routing Tags
| Tag | Purpose |
|-----|---------|
| SOLVSTRAT | Client services & delivery |
| QHORD | Product GTM & launch (June 2026) |
| ASHRAYA | Church admin, operations, finances |
| PERSONAL | Family, home, health, spiritual, journaling |
| CRAYON | Company governance, legal, tax, umbrella entity |

## Critical Conventions

### Time Handling
- All timestamps use **IST (UTC+05:30)**
- Use `format_rfc3339()` in `core/services/google_service.py` to sanitize times
- Format: `YYYY-MM-DDTHH:MM:SS+05:30`

### Security
- Pulse endpoints validate `PULSE_SECRET` (header `x-pulse-secret`) and HMAC `X-Rhodey-Signature`
- Frontend-facing endpoints (`/api/messages`, `/api/calendar-events`, `/api/tasks/*`, `/api/send-message`, `/api/send-draft`, `/api/email-action`) require `X-API-Key` header matching `API_SECRET_KEY` (constant-time comparison via `hmac.compare_digest`). No auth on `/api/webhook`, `/api/pulse` (has its own), or `/` health check.
- Supabase uses service role key (bypasses RLS)

### Pulse Cron Schedule (UTC, matches `.github/workflows/pulse.yml`)
- **Main briefing**: Weekdays `30 23 * * 1-5` + `0 2,6,9,12 * * 1-5` (5AM, 7:30AM, 11:30AM, 2:30PM, 5:30PM IST); Weekends `30 2,9 * * 0,6` (8AM, 3PM IST)
- **Decision Pulse** (no AI, pending approvals): Every 30 min via cron-job.org → `POST /api/decision-pulse`. Auth: `x-pulse-secret` header. See cron-job.org setup below.
- **Sentinel Nudge** (upcoming event watcher): Every 5 min via cron-job.org → `POST /api/sentinel`. Lookahead: 60 min (nudges 0–45 min before). Auth: `x-pulse-secret` header. See cron-job.org setup below.

### External Cron Jobs (cron-job.org Setup)

Some workflows use an **external cron service** because GitHub Actions free plan and Vercel Hobby plan both throttle high-frequency schedules. [cron-job.org](https://cron-job.org) is free and reliable.

**Auth:** Endpoints accept `x-pulse-secret` header matching the `PULSE_SECRET` environment variable. Returns 401 if missing.

**Env vars required (already in Vercel):** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GEMINI_API_KEY`, `PULSE_SECRET`

| Job | URL | Schedule | Timeout |
|---|---|---|---|
| **Sentinel Nudge** | `https://integrated-os.vercel.app/api/sentinel` | `*/5 * * * *` (every 5 min) | 30s |
| **Decision Pulse** | `https://integrated-os.vercel.app/api/decision-pulse` | `*/30 * * * *` (every 30 min) | 30s |
| **Evening Roundup** | `https://integrated-os.vercel.app/api/roundup` | `0 14,20 * * *` (Assuming Asia/Kolkata timezone in cron-job.org: 2PM, 8PM) | 30s |

**Setup:**
1. Go to [cron-job.org](https://cron-job.org) → sign up (free)
2. Create a new cron job for each row above:
   - **URL**: as shown
   - **Schedule**: as shown
   - **Method**: GET
   - **Headers**: Add `x-pulse-secret` with value matching your `PULSE_SECRET` env var
   - **Timeout**: as shown
3. Save and enable

### AI Briefing Rules
- NEVER create tasks from URLs unless explicitly commanded
- NEVER mark tasks done unless input explicitly matches
- Return empty arrays if no explicit commands in inputs
- Filter tasks by 2-day horizon, 14-day creation window
- **Recurring tasks**: `done` skips this week's instance (series continues). `cancelled` ends the series. If reschedule is ambiguous, ask via `ask_user_approval`.
- **Commitment highlighting**: Tasks with `direction=outbound` are Danny's promises to others — highlight them. Tasks with `direction=waiting_on` are blockers — flag them. Show `committed_to` name when available.
- **Schedule queries** ("meetings this week?"): Route to QUERY (interrogate_brain), not DAILY_BRIEF. Calendar events are tagged [PAST] in Python before LLM input. The current time (IST) is injected into the prompt. Never invent headings like "Immediate Priorities" or "Today's Bottleneck" — answer the question directly first, then optional Context section.
- **Query responses**: Max 600 tokens. Answer first (factual), Context section second (bottlenecks/patterns). Never self-analyze or repeat data after Context section.
- **URL quarantine**: Any text containing http/https is saved as a resource only, never as a memory or graph entity. backfill_graph.py fetch_memories() filters URL memories; quick_process.py routes URL NOTES to resources only; entity_extractor.py returns early if text contains URL.

### Data Deletion Safety (Non-Negotiable)
- **NEVER delete any database records (people, tasks, graph_nodes, etc.) without explicit user approval.** Present what would be deleted and ask before executing.
- This applies to: `DELETE` queries, marking records as pruned/removed, and cascade deletions.
- Always use `--dry-run` mode first and show the user what will be affected before running destructive operations.

### Code Quality (Ruff)
- **Always run `ruff check .` after making any code changes.** This project uses `ruff` to catch undefined variables, missing imports, and other linting errors before they crash in production. Fix any *newly introduced* errors immediately.

### Documentation & Spec-Kit Adherence (Definition of Done)
- **Living Documentation is Non-Negotiable:** Your task is NOT complete until the documentation matches the code. Perform documentation updates as the final "wrap-up" step of a feature's lifecycle, right before asking the user for permission to run `git commit` and `git push`. Do not update documentation after every minor code tweak during active development.
- `product-summary/`: Keep local markdown files synchronized. If adding a new feature, create a new `XX-<topic>.md` file and update the README table of contents.
- `.speckit/`: Review and update `speckit.specify.md`, `speckit.plan.md`, and `speckit.tasks.md` to ensure the architectural intent and backlog match reality.
- `.specify/`: Update templates or workflows if the core development loop has changed.
- *Violation example:* The completion handler was once committed without any `product-summary/` update. This is now explicitly forbidden by `.speckit/speckit.constitution.md`. Documentation is part of "Done".

### Token Awareness
- **Warn before heavy operations.** Before reading 5+ files at once, running extensive multi-file search operations, or any action expected to consume significant context, flag it to the user: "This will read N files / search across N paths — may be token-heavy."
- **Prefer targeted reads over bulk.** Use `grep` + `read` with specific line ranges instead of reading entire large files blindly.
- **Session length check.** If a session exceeds ~20 tool calls or is trending toward high context, proactively suggest the user start a fresh session for cleaner context.

### Canonical Import Paths (DRY — Non-Negotiable)

**Every new file MUST use these imports. NEVER duplicate the underlying logic.**

| Need | Canonical Import |
|------|-----------------|
| Supabase client | `from core.services.db import get_supabase` |
| Gemini client | `from core.llm.client import get_gemini_client` |
| Google credentials | `from core.services.google_service import get_google_creds` |
| LLM call with fallback (async) | `from core.llm.fallback import generate_content_with_fallback` |
| LLM call with fallback (sync) | `from core.llm.compat import call_llm_with_fallback_sync` |
| LLM call with retry (async, compat) | `from core.llm.compat import call_gemini_with_retry` |
| Embedding (async) | `from core.llm import get_embedding` |
| Embedding (sync) | `from core.llm.compat import get_embedding_sync` |
| Model constants | `from core.llm.constants import CLASSIFICATION_MODEL, SYNTHESIS_MODEL, EMBEDDING_MODEL, GEMMA_FALLBACK_MODEL, OPENROUTER_MODEL, EMBEDDING_DIMENSION` |
| Retry error lists | `from core.llm.constants import RETRYABLE_ERRORS, NON_RETRYABLE_ERRORS` |
| Retry backoff | `from core.llm.retry import get_jittered_backoff` |
| Pending decision handler | `from core.webhook.utils import process_channel_pending_decision` |
| Audit logging | `from core.lib.audit_logger import log_audit, audit_log_sync, info, error` |
| Google service builder | `from core.services.google_service import get_service` |
| Time formatting | `from core.services.google_service import format_rfc3339` |
| Task/calendar sync | `from core.services.google_service import sync_to_google, sync_to_calendar, get_tasks_service, delete_calendar_event` |
| Multi-key Gemini clients | `from core.llm.client import get_gemini_clients` (returns list of clients from all configured keys) |
| Associative retrieve | `from core.retrieval.search import associative_retrieve` |
| Associative retrieve (compat) | `from core.retrieval.search import search_memories_compat` |
| Forward indexing | `from core.retrieval.pipeline import index_memory, schedule_index_memory` |
| Phrase node graph | `from core.retrieval.graph import upsert_phrase_node, get_subgraph_edges, update_node_stats` |
| Entity extraction | `from core.retrieval.extractor import extract_triples` |
| Memory ranking | `from core.retrieval.ranking import rank_memories` |
| Pagerank | `from core.retrieval.ppr import personalized_pagerank` |
| Redis cache | `from core.lib.redis_cache import cache_get, cache_set` |
| Embedding with multi-key failover | `from core.llm.embedding import get_embedding` |
| Retrieval config | `from core.retrieval import config` |

### Required Environment Variables
```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY
GEMINI_API_KEY_2  # Optional: secondary key for multi-key failover (2x quota)
GEMINI_API_KEY_3  # Optional: tertiary key for multi-key failover (3x quota)
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
PULSE_SECRET
GOOGLE_REFRESH_TOKEN
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_SHEET_ID  # Used in archive ingest and pulse
OPENROUTER_API_KEY  # Fallback for LLM calls (backfill_graph, pulse)
OPENROUTER_BASE_URL  # Default: https://openrouter.ai/api/v1/chat/completions
PULSE_HTTP_REFERER  # Default: http://localhost:8000
PULSE_APP_NAME  # Default: Pulse
API_SECRET_KEY  # Shared secret for frontend API auth (X-API-Key header)
WHATSAPP_INGEST_SECRET  # Shared secret for WhatsApp ingest (X-Ingest-Secret header)
UPSTASH_REDIS_REST_URL  # Required for retrieval Redis cache
UPSTASH_REDIS_REST_TOKEN  # Required for retrieval Redis cache
```

## Integrated AI Tooling

This project is augmented with one local, persistent AI tool that opencode (and its sub-agents) should actively use:

### AgentMemory (Persistent Session Memory)
* **Purpose:** Retains context, user preferences, past debugging steps, and architectural decisions across different terminal sessions.
* **Usage:** Automatically captures tool usage and outputs. The agent should proactively use `memory_smart_search` to recall past context instead of asking the user to re-explain the stack.
* **Status:** Runs persistently as a local PM2 background process (`agentmemory-server`) on `http://localhost:3111`. (Real-time viewer available at `http://localhost:3113`).


## opencode Configuration Architecture (Project-Level)

This project contains local opencode configuration that overrides or extends the global config.

**Project Config (`Integrated-OS/`):**
- `opencode.json` — Git safety guardrails (push=deny, commit=ask)
- `.opencode/command/speckit.*.md` — Auto-discovered Speckit commands
- `.opencode/skills/*/` — Auto-discovered project skills
- `AGENTS.md` — Project domain knowledge

## Testing
- CI: GitHub Actions (`workflow_dispatch` in `.github/workflows/pulse.yml`)
- Local: Send POST to `/api/pulse` with header `x-pulse-secret: <PULSE_SECRET>`
- Local testing uses `pytest` if test files exist, but rely on `ruff check .` for static analysis.

## Vercel Deployment Safety

### Vercel Config Modification (Non-Negotiable)
- **NEVER make any changes to `vercel.json` without explicit, direct approval from the user.** Because this repository uses dual-project routing, unapproved changes to this file can instantly break production routing for multiple applications.

### Two Projects, Separate Config
This repo has **two Vercel projects** linked to the same GitHub repo:
- **`integrated-os`** (backend): Root Directory = `.`, Python FastAPI, uses root `vercel.json` with `rewrites` + `functions`
- **`integrated-os-frontend`** (frontend): Root Directory = `frontend/`, Next.js, no `vercel.json` (auto-detected)

**Important**: `API_SECRET_KEY` must be set as an environment variable in **both** Vercel projects — the backend reads it for auth, and the frontend proxies need it to forward the `X-API-Key` header.

### Critical: `routes` vs `rewrites` in `vercel.json`
- `routes` = **platform-level** — applied globally to ALL projects in the repo. Changes here can break other projects.
- `rewrites` = **build-level** — scoped to the project's build output. Safe to use per project.

**Rule**: Always use `rewrites` (not `routes`) in `vercel.json`. A catch-all `routes` pattern broke the frontend by routing all requests to `api/index.py` across both projects.

### Preview Deployments for Changes
Before pushing to `main`, use branch deployments to test changes without breaking production:
```bash
git checkout -b feat/my-change
# make changes, commit, push
git checkout main
# Vercel auto-deploys preview URL for the branch
```
This applies to: `vercel.json` changes, env vars, build config, framework upgrades.

### One Config Per Project Principle
- **Backend config**: root `vercel.json` (uses `rewrites` + `functions` for Python runtime)
- **Frontend**: No `vercel.json` needed (Next.js auto-detected), or its own `frontend/vercel.json`
- Never share `routes` across projects — they're platform-level, not project-level

### Safe Deployment Checklist
When making infrastructure changes:
1. [ ] Does this modify `vercel.json`, `.vercelignore`, or build config?
2. [ ] Have I checked what other Vercel projects share this repo?
3. [ ] Could `routes` or `builds` affect other projects?
4. [ ] Use a preview/branch deployment to test first
5. [ ] Check build logs for warnings (e.g., "builds existing in config" warning)
6. [ ] Verify both frontend AND backend still work after deployment

## Agent Skills Configuration (Matt Pocock Skills & General)

**For all skills (like /diagnose, /tdd, /to-prd, /to-issues, /grill-with-docs), strictly adhere to the following project conventions:**

1. **Issue Tracker & Workflow (Conversational + Spec-Kit):** 
   - **DO NOT** ask for GitHub Issue numbers, Jira tickets, or Linear links.
   - The user's primary bug-reporting workflow is conversational (via chat logs).
   - When the user triggers `/to-prd` or `/to-issues`, **DO NOT** create a GitHub issue. Instead, write the resulting spec or tasks directly into `.speckit/speckit.specify.md` and `.speckit/speckit.tasks.md`.

2. **Debugging Process (/diagnose):**
   - When the user pastes an error log (e.g., from `audit_logs` in Supabase), immediately jump to the "Hypothesize" and "Verify" steps.
   - Use the explore subagent or grep to trace the data flow and verify your hypothesis before proposing a fix.

3. **Domain Documentation & ADRs:**
   - The "Shared Domain Language" and high-level "Architectural Decisions" are stored in the `product-summary/` folder.
   - When using `/grill-with-docs` or discussing new features, validate your assumptions against the `product-summary/` markdown files.
   - Propose updates to the relevant `product-summary/` file rather than creating a new `docs/adr/` folder.

4. **Session Memory:**
   - Use the `agentmemory` MCP tool to recall past context and save new architectural decisions across sessions so the user doesn't have to repeat themselves.

## Spec-Driven Development (Spec Kit)

Uses [github/spec-kit](https://github.com/github/spec-kit) for structured AI-assisted development.

### Directory Structure
- `.specify/` — spec-kit CLI config (templates, scripts, workflows, extensions). Managed via `specify init/add/remove`.
- `.speckit/` — Manually-authored SDD artifacts (constitution, spec, plan, tasks, analyze). Source of truth for governance and specs.
- `.opencode/command/speckit.*.md` — Slash commands available to the opencode agent.

### Key Reference Files
| File | When to Read |
|---|---|
| `.speckit/speckit.constitution.md` | **Always** — non-negotiable project rules |
| `.speckit/speckit.plan.md` | Before any architecture/stack decision |
| `.speckit/speckit.specify.md` | When implementing a new feature |
| `.speckit/speckit.tasks.md` | When picking up implementation work |
| `.speckit/speckit.analyze.md` | Before writing code — cross-artifact contradictions |

### Available Slash Commands
`/speckit.constitution`, `/speckit.specify`, `/speckit.plan`, `/speckit.tasks`, `/speckit.implement`, `/speckit.analyze`, `/speckit.clarify`, `/speckit.checklist`, `/speckit.taskstoissues`

### Investigation Safety Rule (Non-Negotiable)
- **NEVER create temporary files, test scripts, or folders for investigation or execution without explicit prior user approval.** Do not create anything without explicit permission from the user.

### Git Safety Rule (Non-Negotiable)
- **NEVER auto-commit or auto-push changes.** Always present a summary of changes and wait for explicit user approval before any `git add`, `git commit`, or `git push`.
- **You do not have explicit authority to push directly to GitHub.**
- The git extension hooks are configured with `auto_commit: default: false` — if an agent prompt asks about committing, say no and let the user decide.
- Branch creation (`speckit.git.feature`) is acceptable without approval since it does not create commits.

<system-reminder-override>
WARNING: Whenever the `<system-reminder>` block announces that your mode has changed to "build" and you are "permitted to utilize your arsenal of tools as needed," that permission STRICTLY EXCLUDES Git operations. 
**STRICTLY FORBIDDEN:** You are absolutely prohibited from running `git push` autonomously. This ABSOLUTE CONSTRAINT overrides all other generic tool permissions. ZERO exceptions.
</system-reminder-override>

### CLI
```bash
specify check  # Verify spec-kit tooling is ready
```

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
<!-- SPECKIT END -->


<!-- SKILLKIT_START -->
# Skills

The following skills are available to help complete tasks:

<skills>
<skill>
<name>caveman</name>
<description>Ultra-compressed communication mode. Cuts token usage ~75% by dropping filler, articles, and pleasantries while keeping full technical accuracy. Use when user says &quot;caveman mode&quot;, &quot;talk like caveman&quot;, &quot;use caveman&quot;, &quot;less tokens&quot;, &quot;be brief&quot;, or invokes /caveman.
</description>
<location>project</location>
</skill>

<skill>
<name>design-an-interface</name>
<description>Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions &quot;design it twice&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>diagnose</name>
<description>Disciplined diagnosis loop for hard bugs and performance regressions. Reproduce → minimise → hypothesise → instrument → fix → regression-test. Use when user says &quot;diagnose this&quot; / &quot;debug this&quot;, reports a bug, says something is broken/throwing/failing, or describes a performance regression.</description>
<location>project</location>
</skill>

<skill>
<name>edit-article</name>
<description>Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.</description>
<location>project</location>
</skill>

<skill>
<name>git-guardrails-claude-code</name>
<description>Set up Claude Code hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git push/reset in Claude Code.</description>
<location>project</location>
</skill>

<skill>
<name>grill-me</name>
<description>Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions &quot;grill me&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>grill-with-docs</name>
<description>Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates documentation (CONTEXT.md, ADRs) inline as decisions crystallise. Use when user wants to stress-test a plan against their project&apos;s language and documented decisions.</description>
<location>project</location>
</skill>

<skill>
<name>handoff</name>
<description>Compact the current conversation into a handoff document for another agent to pick up.</description>
<location>project</location>
</skill>

<skill>
<name>improve-codebase-architecture</name>
<description>Find deepening opportunities in a codebase, informed by the domain language in CONTEXT.md and the decisions in docs/adr/. Use when the user wants to improve architecture, find refactoring opportunities, consolidate tightly-coupled modules, or make a codebase more testable and AI-navigable.</description>
<location>project</location>
</skill>

<skill>
<name>karpathy-guidelines</name>
<description>Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.</description>
<location>project</location>
</skill>

<skill>
<name>migrate-to-shoehorn</name>
<description>Migrate test files from `as` type assertions to @total-typescript/shoehorn. Use when user mentions shoehorn, wants to replace `as` in tests, or needs partial test data.</description>
<location>project</location>
</skill>

<skill>
<name>obsidian-vault</name>
<description>Search, create, and manage notes in the Obsidian vault with wikilinks and index notes. Use when user wants to find, create, or organize notes in Obsidian.</description>
<location>project</location>
</skill>

<skill>
<name>prototype</name>
<description>Build a throwaway prototype to flesh out a design before committing to it. Routes between two branches — a runnable terminal app for state/business-logic questions, or several radically different UI variations toggleable from one route. Use when the user wants to prototype, sanity-check a data model or state machine, mock up a UI, explore design options, or says &quot;prototype this&quot;, &quot;let me play with it&quot;, &quot;try a few designs&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>qa</name>
<description>Interactive QA session where user reports bugs or issues conversationally, and the agent files GitHub issues. Explores the codebase in the background for context and domain language. Use when user wants to report bugs, do QA, file issues conversationally, or mentions &quot;QA session&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>request-refactor-plan</name>
<description>Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.</description>
<location>project</location>
</skill>

<skill>
<name>review</name>
<description>Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo&apos;s documented coding standards?) and Spec (does the code match what the originating issue/PRD asked for?). Runs both reviews in parallel sub-agents and reports them side by side. Use when the user wants to review a branch, a PR, work-in-progress changes, or asks to &quot;review since X&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>scaffold-exercises</name>
<description>Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section.</description>
<location>project</location>
</skill>

<skill>
<name>setup-matt-pocock-skills</name>
<description>Sets up an `## Agent skills` block in AGENTS.md/CLAUDE.md and `docs/agents/` so the engineering skills know this repo&apos;s issue tracker (GitHub or local markdown), triage label vocabulary, and domain doc layout. Run before first use of `to-issues`, `to-prd`, `triage`, `diagnose`, `tdd`, `improve-codebase-architecture`, or `zoom-out` — or if those skills appear to be missing context about the issue tracker, triage labels, or domain docs.</description>
<location>project</location>
</skill>

<skill>
<name>setup-pre-commit</name>
<description>Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typechecking/testing.</description>
<location>project</location>
</skill>

<skill>
<name>supabase</name>
<description>Use when doing ANY task involving Supabase. Triggers: Supabase products (Database, Auth, Edge Functions, Realtime, Storage, Vectors, Cron, Queues); client libraries and SSR integrations (supabase-js, @supabase/ssr) in Next.js, React, SvelteKit, Astro, Remix; auth issues (login, logout, sessions, JWT, cookies, getSession, getUser, getClaims, RLS); Supabase CLI or MCP server; schema changes, migrations, security audits, Postgres extensions (pg_graphql, pg_cron, pg_vector).</description>
<location>project</location>
</skill>

<skill>
<name>supabase-postgres-best-practices</name>
<description>Postgres performance optimization and best practices from Supabase. Use this skill when writing, reviewing, or optimizing Postgres queries, schema designs, or database configurations.</description>
<location>project</location>
</skill>

<skill>
<name>tdd</name>
<description>Test-driven development with red-green-refactor loop. Use when user wants to build features or fix bugs using TDD, mentions &quot;red-green-refactor&quot;, wants integration tests, or asks for test-first development.</description>
<location>project</location>
</skill>

<skill>
<name>to-issues</name>
<description>Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices. Use when user wants to convert a plan into issues, create implementation tickets, or break down work into issues.</description>
<location>project</location>
</skill>

<skill>
<name>to-prd</name>
<description>Turn the current conversation context into a PRD and publish it to the project issue tracker. Use when user wants to create a PRD from the current context.</description>
<location>project</location>
</skill>

<skill>
<name>triage</name>
<description>Triage issues through a state machine driven by triage roles. Use when user wants to create an issue, triage issues, review incoming bugs or feature requests, prepare issues for an AFK agent, or manage issue workflow.</description>
<location>project</location>
</skill>

<skill>
<name>ubiquitous-language</name>
<description>Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glossary, harden terminology, create a ubiquitous language, or mentions &quot;domain model&quot; or &quot;DDD&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>write-a-skill</name>
<description>Create new agent skills with proper structure, progressive disclosure, and bundled resources. Use when user wants to create, write, or build a new skill.</description>
<location>project</location>
</skill>

<skill>
<name>writing-beats</name>
<description>Shape an article as a journey of beats, choose-your-own-adventure style. The user picks a starting beat from the raw material, you write only that beat, then offer options for where to pivot next, beat by beat, until the article reaches a natural end. Use when the user has raw material and wants to assemble it as a narrative rather than an argument.</description>
<location>project</location>
</skill>

<skill>
<name>writing-fragments</name>
<description>Grilling session that mines the user for fragments — heterogeneous nuggets of writing (claims, vignettes, sharp sentences, half-thoughts) — and appends them to a single document as raw material for a future article. Use when the user wants to develop ideas before imposing structure, or mentions &quot;fragments&quot;, &quot;ideate&quot;, or &quot;raw material&quot; for writing.</description>
<location>project</location>
</skill>

<skill>
<name>writing-shape</name>
<description>Take a markdown file of raw material and shape it into an article through a conversational session — drafting candidate openings, growing the piece paragraph by paragraph, arguing about format (lists, tables, callouts, quotes) at each step. Use when the user has a pile of notes, fragments, or a rough draft and wants help turning it into something publishable.</description>
<location>project</location>
</skill>

<skill>
<name>zoom-out</name>
<description>Tell the agent to zoom out and give broader context or a higher-level perspective. Use when you&apos;re unfamiliar with a section of code or need to understand how it fits into the bigger picture.</description>
<location>project</location>
</skill>
</skills>

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->