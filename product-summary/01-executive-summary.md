# 1. Executive Summary

> Updated 2026-08-15. Numbers below are verified against the live system; see
> `99-architecture-reference.md` and `05-database-schema.md` for depth.

## What Is Integrated-OS?

Integrated-OS is a proprietary AI-powered Chief of Staff — an Executive Command
Center for one person's life. It bridges the gap between raw input (voice notes,
text messages, photos, PDFs, emails, Teams/WhatsApp/Outlook) and strategic
execution (Google Calendar, Google Tasks, a knowledge graph, and judgment-timed
AI briefings). Since M3 (Aug 2026) it is **multi-tenant**: each tenant is fully
isolated by `owner_id` scoping with per-tenant LLM spend caps and OTP/Google
sign-in — no API-key pasting.

## By the Numbers (verified 2026-08-15)

| Metric | Value |
|--------|-------|
| Source lines of code | ~133,000 (Python ~86k + Flutter ~21k + frontend ~18k + SQL ~7k) |
| Database tables | **59** |
| External API integrations | 10+ (Supabase, Gemini, Telegram, Gmail, Google Calendar, Google Tasks, Outlook, Teams, Jina AI, FCM, Upstash Redis) |
| LLM providers | Gemini (classification + synthesis) with fallback chain |
| Flutter app tests | 62 (incl. on-device integration tests) |
| Python test suite | 865 tests across 5 layers + 13 aspect markers |
| Infrastructure | Modal (Python 3.11 FastAPI), Supabase, Upstash Redis |

## The Architecture (5 Layers + Infrastructure)

1. **Ingestion Layer**: Receives messages from all channels (Telegram, WhatsApp
   via Beeper, Email, Outlook, Teams, Calls) through a unified `ingest()`
   contract with **direction-awareness** (your own sends never surface as
   inbound items). Classifies intent via Gemini Flash Lite, quarantines URLs at
   ingress, and deduplicates at multiple levels.

2. **Processing Layer**: Routes classified intents through the unified Action
   Planner (`plan_actions()` → `execute_planned_actions()`) with typed
   operations and typed contracts. Entity resolution happens BEFORE creation via
   deterministic `resolve_entities()`. Enrichment is queued via
   `pending_enrichment_jobs` (survives cold starts on Modal).

3. **Intelligence Layer**: Hybrid retrieval (vector + knowledge graph) with
   multi-signal ranking. Knowledge graph with typed nodes and edges — all edges
   flow through HITL approval with per-item undo. Context registry with 6
   strategies. **Brain synthesis is per-tenant**, anchored on the
   `conversation_threads.active_anchor`, not at an "organization" level (the org
   concept was dropped — see db/75).

4. **Presentation Layer**: The Pulse generates AI briefings with a single LLM
   call and write-behind pattern. Decision Pulse collects pending approvals.
   Sentinel watches for upcoming events and runs background jobs as piggybacks.
   **Every user decision — approve/reject/snooze/correct — persists to the
   `decisions` ledger and trains subsystem patterns** (the learning loop).

5. **Surface Layer**: **Telegram (primary)** + email + Teams + WhatsApp via
   Beeper + the **Flutter app (Rhodey)** with onboarding, personas, home-screen
   modes, voice capture, a Telegram-independent reply path, and push
   notifications. A Next.js dashboard exists but is parked.

**Infrastructure (Cross-Cutting)**: Modal (deploy), Supabase (Postgres), Google
Calendar/Tasks/Gmail APIs, Telegram + FCM push, GitHub Actions CI/CD with
fast/nightly test gates, Upstash Redis (rate limiter + test sandbox lock).

## What Makes It Unique

- **Multimodal capture**: voice note, photo, PDF, DOCX, or text — all becomes structured data
- **Learning loop**: every approve/reject/snooze/correct trains per-subsystem patterns — "Not now" never silently resets
- **Judgment over volume**: home-screen modes (proceed/decide/sprint/catch-up/wrap) and snooze escalation ladders decide *what to show when*
- **Passive intelligence**: practice detection, after-action review, focal-item intelligence with transparency reports
- **Self-healing infrastructure**: dead letter queues, zombie recovery, LLM fallback chains, clean-slate test isolation
- **Multi-tenant by design**: per-tenant isolation, spend caps, sign-in lifecycle (Google / email-OTP)
- **Knowledge graph + vector hybrid**: both, with parallel multi-signal queries and per-item undo on graph decisions
- **Comprehensive test gate**: fast + nightly tiers, aspect markers, cross-tenant leak guard — every change lands behind it
- **Conversational state engine**: persistent threads, workflow state, active-anchor carry-forward
- **Temporal lineage**: versioned records (`supersedes_id`, `is_current`) with soft-delete semantics
