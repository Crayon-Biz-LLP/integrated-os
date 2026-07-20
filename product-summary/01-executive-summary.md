# 1. Executive Summary

## What Is Integrated-OS?

Integrated-OS is a proprietary AI-powered Personal Operating System — an Executive Command Center designed for one person's life. It bridges the gap between raw input (voice notes, text messages, photos, PDFs, emails, journal entries) and strategic execution (Google Calendar, Google Tasks, knowledge graphs, and AI-generated situation reports).

This is not a SaaS product or a generic productivity app. It is a bespoke, hyper-personalized system built by one person for themselves, spanning their businesses, family life, spiritual practices, and personal growth.

## By the Numbers

| Metric | Value |
|--------|-------|
| Source lines of code | ~20,000 (Python + TypeScript) |
| Database tables | 35+ |
| External API integrations | 10+ (Supabase, Gemini, Telegram, Gmail, Google Calendar, Google Tasks, Outlook, Jina AI, OpenRouter, FCM) |
| Autonomous runs per week | 210+ |
| LLM providers | 3 (Gemini, Gemma, OpenRouter) |
| Frontend components | 88+ |
| Infrastructure cost | $0 (free tiers) |

## The Architecture (5 Layers + Infrastructure)

The system operates as 5 vertical pipeline layers atop a shared infrastructure layer:

1. **Ingestion Layer**: Receives messages from all channels (Telegram, WhatsApp, Email, Outlook, Teams, Calls) through a unified `ingest()` contract. Classifies intent via Gemini Flash Lite, quarantines URLs at ingress, and deduplicates at 3 levels.

2. **Processing Layer**: Routes classified intents through the unified Action Planner (`plan_actions()` → `execute_planned_actions()`). Executes typed operations (create_task, close_task, reschedule, cancel_recurring) via direct DB operations. Entity resolution happens BEFORE creation via deterministic `resolve_entities()` linker. Enrichment queued via `pending_enrichment_jobs` to survive Vercel cold kills. Formal state machines guard all 16 tables.

3. **Intelligence Layer**: Associative retrieval with 7-signal ranking (semantic, PPR, recency, importance, project, specificity, person_boost). Knowledge graph with 5 node types and 16 edge types — all edges flow through HITL approval. Context registry with 6 strategies and entity-grounded gates. Brain synthesis at the organization level.

4. **Presentation Layer**: Pulse Engine generates AI briefings via a single LLM call (no agent loop) with write-behind pattern. Decision Pulse collects pending approvals without AI. Sentinel watches for upcoming events and runs 7 background jobs as piggybacks. Consolidated health monitor.

5. **Surface Layer**: Telegram bot (active, planning deprecation), Web UI Dashboard (Next.js), Flutter Mobile App (Rhodey) with full chat, voice, and push notification support.

**Infrastructure (Cross-Cutting)**: Supabase database, Google Calendar/Tasks/Gmail APIs, Telegram + FCM push, GitHub Actions CI/CD, cron-job.org for high-frequency schedules, Upstash Redis for caching.

## What Makes It Unique

- **Multimodal capture**: Send a voice note, photo, PDF, DOCX, or text — it all becomes structured data
- **Passive intelligence**: Discovers connections you didn't see (serendipity engine), detects habits from raw text (practice detection), reviews its own day (after-action report)
- **Self-healing infrastructure**: Dead letter queues, zombie recovery, triple LLM fallback, enrichment queue that survives cold kills, 300+ error guards
- **Zero infrastructure cost**: Runs on Vercel free tier, GitHub Actions free minutes, Gemini free API
- **Hyper-personalized**: 7 org routing tags span all domains of one person's life
- **Temporal lineage**: Every record is versioned and append-only via DB triggers — time-travel to see any task, memory, or project at any point in the past
- **Knowledge graph + vector search hybrid**: Most systems use one or the other — Integrated-OS uses both, with parallel multi-signal queries
- **Conversational state engine**: Persistent threads, workflow state, active anchor carry-forward, bounded classify context
- **Entity-grounded context**: Context Registry prevents hallucination by gating retrieval to confirmed entities
