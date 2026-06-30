# Integrated-OS: Product Summary

A comprehensive guide to the Integrated-OS system — an AI-powered Personal Operating System that serves as an Executive Command Center for one person's life, work, and spiritual journey.

## Contents

### The Story
| File | What It Covers |
|------|---------------|
| [01-executive-summary.md](01-executive-summary.md) | One-page system overview, key stats, what makes it unique |
| [02-origin-philosophy.md](02-origin-philosophy.md) | Origin story, product philosophy, design values |
| [03-architecture-overview.md](03-architecture-overview.md) | System architecture, triangular engine, data flow |
| [04-backend-frontend.md](04-backend-frontend.md) | Python/FastAPI backend, Next.js/React frontend, deployment |
| [04b-intelligence-tiers.md](04b-intelligence-tiers.md) | Rhodey's 4-tier intelligence architecture — pulse, context, memory, session working memory |
 
### The Database
| File | What It Covers |
|------|---------------|
| [05-database-schema.md](05-database-schema.md) | Complete 21-table schema, indexes, RPCs, foreign keys |

### Intake & Input Channels
| File | What It Covers |
|------|---------------|
| [06-telegram-intake.md](06-telegram-intake.md) | Telegram webhook, dedup, auth, routing |
| [07-multimodal-classification.md](07-multimodal-classification.md) | Intent classification, multimodal processing, stealth routing |
| [08-input-channels.md](08-input-channels.md) | Journal pipeline, Gmail, Outlook, staging sorter, QuickChat, QuickCommand |
| [08b-telegram-commands.md](08b-telegram-commands.md) | All 18 Telegram commands with syntax and behavior |
| [25-whatsapp-ingest.md](25-whatsapp-ingest.md) | WhatsApp notification ingest via MacroDroid, separate table, approval flow |
| [26-call-recording-ingest.md](26-call-recording-ingest.md) | Call transcription pipeline, Google Drive webhooks, and AI task extraction |

### The Task System
| File | What It Covers |
|------|---------------|
| [09-task-creation-paths.md](09-task-creation-paths.md) | 3+1 task creation paths, lifecycle, Google sync |
| [10-task-to-project-assignment.md](10-task-to-project-assignment.md) | 7-stage project cascade, people assignment, graph edges |
| [11-people-project-autocreation.md](11-people-project-autocreation.md) | AI-gated creation, blocklist, dedup, graph-table bridge |

### The Pulse Engine
| File | What It Covers |
|------|---------------|
| [12-pulse-engine-overview.md](12-pulse-engine-overview.md) | Crown jewel overview, briefing cycle, orchestration |
| [13-pulse-engine-compass-personas.md](13-pulse-engine-compass-personas.md) | Compass opening, 5 briefing personas, horizon guard |
| [14-pulse-engine-agents-prompt.md](14-pulse-engine-agents-prompt.md) | Multi-agent orchestration, 250-line prompt, write phase |

### Intelligence Layer
| File | What It Covers |
|------|---------------|
| [15-llm-architecture.md](15-llm-architecture.md) | Gemini models, triple fallback chain, model registry, rate limiting |
| [16-memory-knowledge-graph.md](16-memory-knowledge-graph.md) | Memory system, hybrid search, knowledge graph |
| [17-canonical-brain-synthesis.md](17-canonical-brain-synthesis.md) | Canonical pages, brain synthesis, entity mapping from journal |

### Passive Intelligence
| File | What It Covers |
|------|---------------|
| [18-passive-intelligence.md](18-passive-intelligence.md) | Serendipity engine, adaptive learning, after-action, drift detection |
| [19-practices-rhythms.md](19-practices-rhythms.md) | Practice detection, lifecycle, correlations, rhythms dashboard |

### Email, Dashboard & Operations
| File | What It Covers |
|------|---------------|
| [20-email-pipeline.md](20-email-pipeline.md) | Gmail/Outlook ingest, Gemini classification, draft approval flow |
| [21-frontend-dashboard.md](21-frontend-dashboard.md) | 10 dashboard modules, knowledge graph visualization, design system |
| [22-resilience-self-healing.md](22-resilience-self-healing.md) | Temporal lineage, DLQ, zombie recovery, duplicate guard |
| [23-governance-security.md](23-governance-security.md) | Season context, org tags, CI/CD, security model |

### Intake & Input Channels (continued)
| File | What It Covers |
|------|---------------|
| [27-personal-capture-pipeline.md](27-personal-capture-pipeline.md) | Personal notes, meeting notes, ideas, voice memos — capture from natural speech, /note command, and evening roundup |
| [28-clarification-loop-guards.md](28-clarification-loop-guards.md) | Entity grounding guards, URL quarantine, clarification loop architecture, organizations table, validation window |
| [29-conversation-threads-and-workflows.md](29-conversation-threads-and-workflows.md) | Persistent threads, workflow state engine, query carry-forward across turns |
| [29-conversational-persistence-memory-hygiene.md](29-conversational-persistence-memory-hygiene.md) | Memory expiry, versioning, deletion cleanup, raw dump lifecycle, query carry-forward |
| [30-context-registry-truth-boundary.md](30-context-registry-truth-boundary.md) | Context registry (entity-grounded retrieval, 6 strategies, hard/soft gates) + truth boundary (post-generation claim validation, action receipts) + prompt registry + JSON fail-closed |

### Use Cases
| File | What It Covers |
|------|---------------|
| [24-use-cases.md](24-use-cases.md) | 30 thematic use case stories across all system capabilities |
