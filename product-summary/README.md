# Integrated-OS: Product Summary

An AI-powered Personal Operating System — an Executive Command Center for one person's life, work, and ministry.

## Core Architecture

| File | What It Covers |
|------|---------------|
| [01-executive-summary.md](01-executive-summary.md) | One-page system overview, key stats, what makes it unique |
| [02-origin-philosophy.md](02-origin-philosophy.md) | Origin story, product philosophy, design values |
| [03-architecture-overview.md](03-architecture-overview.md) | 6-layer system architecture, data flow, technology stack |
| [04-backend-frontend.md](04-backend-frontend.md) | Python/FastAPI backend, Next.js/React frontend, Modal deployment |
| [04b-intelligence-tiers.md](04b-intelligence-tiers.md) | Rhodey's 4-tier intelligence architecture — pulse, context, memory, session working memory |

## Input Channels

| File | What It Covers |
|------|---------------|
| [07-multimodal-classification.md](07-multimodal-classification.md) | Intent classification, multimodal processing, stealth routing |
| [08-input-channels.md](08-input-channels.md) | Journal pipeline, Gmail, Outlook, QuickChat, QuickCommand |
| [08b-telegram-commands.md](08b-telegram-commands.md) | All Telegram commands with syntax and behavior |
| [25-whatsapp-ingest.md](25-whatsapp-ingest.md) | WhatsApp notification ingest via MacroDroid, approval flow |
| [26-call-recording-ingest.md](26-call-recording-ingest.md) | Call transcription pipeline, Google Drive webhooks, AI task extraction |

## Processing & Actions

| File | What It Covers |
|------|---------------|
| [51-action-planner-architecture.md](51-action-planner-architecture.md) | Universal Action Planner — unified pipeline for all task/note/event operations |
| [58-final-architecture-overhaul.md](58-final-architecture-overhaul.md) | Complete architecture overhaul (Parts 51-58), definitive reference |
| [10-task-to-project-assignment.md](10-task-to-project-assignment.md) | Project cascade, people assignment, graph edges |
| [11-people-project-autocreation.md](11-people-project-autocreation.md) | AI-gated creation, blocklist, dedup, graph-table bridge |

## Knowledge & Memory

| File | What It Covers |
|------|---------------|
| [16-memory-knowledge-graph.md](16-memory-knowledge-graph.md) | Memory system, hybrid search, knowledge graph |
| [17-canonical-brain-synthesis.md](17-canonical-brain-synthesis.md) | Canonical pages, brain synthesis, entity mapping from journal |
| [28-clarification-loop-guards.md](28-clarification-loop-guards.md) | Entity grounding guards, URL quarantine, clarification loop |
| [29-conversation-threads-and-workflows.md](29-conversation-threads-and-workflows.md) | Persistent threads, workflow state engine, cross-turn context |
| [30-context-registry-truth-boundary.md](30-context-registry-truth-boundary.md) | Context registry (entity-grounded retrieval, 6 strategies) + truth boundary (claim validation) |
| [31-decision-audit.md](31-decision-audit.md) | Structured decision audit, `/why` command, 4-stage reasoning trace |

## Intelligence & Pulse Engine

| File | What It Covers |
|------|---------------|
| [13-pulse-engine-compass-personas.md](13-pulse-engine-compass-personas.md) | Compass opening, 5 briefing personas, horizon guard |
| [15-llm-architecture.md](15-llm-architecture.md) | Gemini models, triple fallback chain, model registry, rate limiting |
| [18-passive-intelligence.md](18-passive-intelligence.md) | Serendipity engine, adaptive learning, after-action, drift detection |
| [19-practices-rhythms.md](19-practices-rhythms.md) | Practice detection, lifecycle, correlations, rhythms dashboard |

## Email, Dashboard & Operations

| File | What It Covers |
|------|---------------|
| [20-email-pipeline.md](20-email-pipeline.md) | Gmail/Outlook ingest, Gemini classification, draft approval flow |
| [21-frontend-dashboard.md](21-frontend-dashboard.md) | Dashboard modules, knowledge graph visualization, design system |
| [23-governance-security.md](23-governance-security.md) | Season context, org tags, CI/CD, security model |
| [38-push-notifications.md](38-push-notifications.md) | FCM push notification service, response text to app, diagnostic endpoints |
| [39-notebooklm-sync.md](39-notebooklm-sync.md) | Google Docs API sync for Notebook LM, CI workflow |

## Database

| File | What It Covers |
|------|---------------|
| [05-database-schema.md](05-database-schema.md) | Complete schema, indexes, RPCs, foreign keys |

## Mobile App

| File | What It Covers |
|------|---------------|
| [48-flutter-app-architecture.md](48-flutter-app-architecture.md) | Flutter app — screens, models, services, Firebase, TTS, voice, in-app updates |

## Infrastructure & Deployment

| File | What It Covers |
|------|---------------|
| [99-architecture-reference.md](99-architecture-reference.md) | Consolidated architecture reference — single-source 6-layer overview with key files and metrics |
| [99-lovable-product-brief.md](99-lovable-product-brief.md) | High-level product brief for external audiences |
| [22b-normalized-label-fix.md](22b-normalized-label-fix.md) | normalized_label column for PostgREST-compatible case-insensitive graph node dedup |

## Recent & Missing Docs

| File | What It Covers |
|------|---------------|
| [14-infrastructure.md](14-infrastructure.md) | Modal deployment, cron jobs, environment variables (NEW) |
| [15-recent-enhancements.md](15-recent-enhancements.md) | Recent bug fixes and enhancements (NEW) |

---

## Related Documentation

- **Session notes** — `session-notes/` — Chronological records of development sessions
- **Plans** — `plans/` — Future implementation plans and migration roadmaps
- **Architecture diagrams** — `docs/` — Archify-generated SVG architecture diagrams
- **Speckit** — `.speckit/` — Spec-driven development artifacts (constitution, plan, specify)
