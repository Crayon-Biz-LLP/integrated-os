# 1. Executive Summary

## What Is Integrated-OS?

Integrated-OS is a proprietary AI-powered Personal Operating System — an Executive Command Center designed for one person's life. It bridges the gap between raw input (voice notes, text messages, photos, emails, journal entries) and strategic execution (Google Calendar, Google Tasks, knowledge graphs, and AI-generated situation reports).

This is not a SaaS product or a generic productivity app. It is a bespoke, hyper-personalized system built by one person for themselves, spanning their businesses, family life, spiritual practices, and personal growth.

## By the Numbers

| Metric | Value |
|--------|-------|
| Source lines of code | 23,700 (12,900 Python + 10,800 TypeScript) |
| Database tables | 21 |
| External API integrations | 9 (Supabase, Gemini, Telegram, Gmail, Google Calendar, Google Tasks, Outlook, Jina AI, OpenRouter) |
| GitHub Actions workflows | 7 |
| Autonomous runs per week | 210+ |
| LLM providers | 3 (Gemini, Gemma, OpenRouter) |
| Error guards (try/except) | 313 across 252 functions |
| Frontend components | 88 |
| Infrastructure cost | $0 (free tiers) |

## The Architecture (6 Layers)

The system operates as 6 vertical layers spanning ingestion to presentation:

1. **Ingestion Layer**: Receives messages from all channels (Telegram, WhatsApp, Email, Outlook, Teams, Calls) through a unified `ingest()` contract. Classifies intent via Gemini, quarantines URLs at ingress (`url_filter.py`), and deduplicates at 3 levels (Telegram update_id, dedup_key, DB constraints).

2. **Processing Layer**: Routes classified intents through the unified Action Planner (`plan_actions()` → `execute_planned_actions()`). Executes typed operations (create_task, close_task, reschedule, cancel_recurring) via direct DB operations (`create_task_direct()`, `create_note_direct()`). Entity resolution happens BEFORE creation via deterministic `resolve_entities()` linker. Enrichment (graph edges, entities, embeddings) is queued via `pending_enrichment_jobs` to survive Vercel cold kills.

3. **Intelligence Layer**: Associative retrieval with 7-signal ranking (semantic, PPR, recency, importance, project, specificity, person_boost). Knowledge graph with 5 node types and 16 edge types — all edges flow through HITL approval via `pending_graph_edges`. Context registry with 6 strategies and entity-grounded gates. Brain synthesis at the organization level.

4. **Presentation Layer**: Pulse Engine generates AI briefings via a single LLM call (no agent loop). Decision Pulse collects pending approvals without AI. Sentinel watches for upcoming events and runs 7 background jobs as piggybacks. Consolidated health monitor (`run_full_health_check()`) checks all subsystems.

5. **Persistence Layer**: 16 formal state machines with documented valid transitions. DB-backed state (clarifications, sessions, workflows survive cold restarts). Temporal lineage via DB triggers on tasks and canonical_pages. `pending_nodes` + `merge_proposals` for graph node approvals.

6. **Integration Layer**: Google Calendar/Tasks sync with 404 auto-heal. Telegram bot with FCM push notifications. cron-job.org for high-frequency schedules (sentinel every 5min, decision pulse every 30min). GitHub Actions for Pulse, backfill, health, and NotebookLM sync.

## What Makes It Unique

- **Multimodal capture**: Send a voice note, photo, PDF, or text — it all becomes structured data
- **Passive intelligence**: It discovers connections you didn't see (serendipity engine), detects habits from raw text (practice detection), and reviews its own day (after-action report)
- **Self-healing infrastructure**: Dead letter queues, zombie recovery, triple LLM fallback, 313 error guards
- **Zero infrastructure cost**: Runs on Vercel free tier, GitHub Actions free minutes, Gemini free API
- **Hyper-personalized**: 7 org routing tags (SOLVSTRAT, PRODUCT_LABS, CRAYON, PERSONAL, ASHRAYA, FAMILY, QHORD) span all domains of one person's life
- **Temporal lineage**: Every record is versioned and append-only — you can time-travel to see what any task, memory, or project looked like at any point in the past
- **Knowledge graph + vector search hybrid**: Most systems use one or the other — Integrated-OS uses both, with parallel multi-signal queries
