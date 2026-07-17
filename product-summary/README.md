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
| [25b-whatsapp-batch-ingest.md](25b-whatsapp-batch-ingest.md) | WhatsApp conversation batching — 3-min window, advisory lock, atomic batch-or-insert RPC |

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
| [30-context-registry-truth-boundary.md](30-context-registry-truth-boundary.md) | Context registry (entity-grounded retrieval, 6 strategies, hard/soft gates) + truth boundary (post-generation claim validation, action receipts) + prompt registry + JSON fail-closed |

### Graph & Knowledge
| File | What It Covers |
|------|---------------|
| [36-graph-cross-domain-linkages.md](36-graph-cross-domain-linkages.md) | Graph cross-domain linkages, multi-layered edge extraction, WORKS_AT/BELONGS_TO pipeline |
| [44-graph-kg-hardening.md](44-graph-kg-hardening.md) | 4-layer KG hardening, concept fluidity (synaptic plasticity), Entities tab UI |
| [45-graph-redesign-dedup.md](45-graph-redesign-dedup.md) | Three-pane graph surface, 2.5D spherical NeuralDisc, 4-layer dedup |

### Meta-Cognitive Layer
| File | What It Covers |
|------|---------------|
| [33-pattern-learning-undo-fixes.md](33-pattern-learning-undo-fixes.md) | Pattern learning fixes: Telegram undo buttons, configurable cross-subsystem blend, entity type-weighted overlap bonus, missing import fixes |

### Postgres / Schema Fixes
| File | What It Covers |
|------|---------------|
| [22b-normalized-label-fix.md](22b-normalized-label-fix.md) | normalized_label column for PostgREST-compatible case-insensitive graph node dedup |

### Enrichment & Workflows
| File | What It Covers |
|------|---------------|
| [37-smart-batch-enrichment.md](37-smart-batch-enrichment.md) | Smart batch enrichment — multi-signal collection, per-signal LLM decision parsing, calendar_event signal type |
| [40-process-input-refactoring.md](40-process-input-refactoring.md) | process_single_dump refactoring, calendar event simplification *(superseded by Action Planner)* |
| [46-role-update-intent.md](46-role-update-intent.md) | ROLE_UPDATE intent — detect role attributions, update people.role, SERVES_AT edges |
| [47-classification-context-boundary.md](47-classification-context-boundary.md) | Bounded classify context, bot receipt stripping, PERSON QUERIES rule |
| [50-multi-intent-task-closure.md](50-multi-intent-task-closure.md) | Multi-intent messages, task closure via enrichment pipeline, secondary_actions in classify |
| [51-action-planner-architecture.md](51-action-planner-architecture.md) | Universal Action Planner replacing single-intent matchers for tasks, recurring series, and calendar operations |
| [52-unified-action-planner-holistic.md](52-unified-action-planner-holistic.md) | *(Deprecated — merged into 58)* Unified Action Planner holistic architecture completion |
| [53-architecture-stabilization.md](53-architecture-stabilization.md) | DB-backed state, formal state machines, unified ingest, URL quarantine module, async webhook queue |
| [54-hardening-trigger-fix-graph-cleanup-push-fix.md](54-hardening-trigger-fix-graph-cleanup-push-fix.md) | close_task_edges trigger crash fix, graph node duplicate cleanup, WhatsApp JSON parse fix, push notification device_tokens table |
| [55-root-cause-enforcement.md](55-root-cause-enforcement.md) | 4W1H root cause enforcement — commit-msg hook, AGENTS.md Step 10, opencode.json commit rules |
| [56-enrichment-queue.md](56-enrichment-queue.md) | Queue-based enrichment replacing fire-and-forget — Vercel-safe graph edges, entity extraction, embeddings |
| [57-architecture-cleanup-and-hardening.md](57-architecture-cleanup-and-hardening.md) | Dead file cleanup, now_ist(), health monitor consolidation, prompt audit |
| [58-final-architecture-overhaul.md](58-final-architecture-overhaul.md) | Complete 6-layer architecture overhaul (Parts 51-58), final architecture diagram, 22-scenario UAT validation |

### Mobile App (Flutter — Rhodey)
| File | What It Covers |
|------|---------------|
| [38-push-notifications.md](38-push-notifications.md) | FCM push notification service, response text to app, diagnostic endpoints |
| [43-apk-versioning.md](43-apk-versioning.md) | Flutter APK build pipeline, in-app update system, versioning |
| [48-flutter-app-architecture.md](48-flutter-app-architecture.md) | Full Flutter app — 12 screens, 5 models, 3 services, 4 widgets, Firebase, TTS, voice, in-app updates |
| [49-rhodey-surface-ux.md](49-rhodey-surface-ux.md) | Rhodey Surface v1→v3 evolution, Horizon/Traces design, App Redesign v2 P1-P5 |

### Notebook LM & Docs
| File | What It Covers |
|------|---------------|
| [39-notebooklm-sync.md](39-notebooklm-sync.md) | Google Docs API sync for Notebook LM, CI workflow, OAuth scope update |

### Infrastructure
| File | What It Covers |
|------|---------------|
| [44b-llm-layer-consolidation.md](44b-llm-layer-consolidation.md) | Consolidated LLM/Supabase/Google clients, multi-key Gemini failover, unified fallback chain |
| [42-temporal-versioning-expansion.md](42-temporal-versioning-expansion.md) | DB-trigger-based temporal lineage for memories, removal of app-level versioning |
| [41-diagnostic-endpoints.md](41-diagnostic-endpoints.md) | /api/briefing-ping, /api/briefing-debug health and debug endpoints |

### Frontend Features
| File | What It Covers |
|------|---------------|
| [32-resource-list-dismiss.md](32-resource-list-dismiss.md) | Resource clusters list view toggle and dismiss/read feature |

### Use Cases
| File | What It Covers |
|------|---------------|
| [24-use-cases.md](24-use-cases.md) | 30 thematic use case stories across all system capabilities |
