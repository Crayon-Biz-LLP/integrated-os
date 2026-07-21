# Rhodey OS — Architecture Reference

> **A single-source reference for the 6-layer architecture of Integrated-OS (Rhodey).**
> Last updated: Jul 21, 2026 (Parts 50-62 hardening complete).
> See `product-summary/` for detailed per-part documentation.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                      INGESTION LAYER                             │
│  Telegram │ WhatsApp │ Email │ Outlook │ Teams │ Calls │ Docs   │
│  → classify() → url_filter() → ingest() → plan_actions()        │
│  ALL channels routed through a single unified pipeline           │
├──────────────────────────────────────────────────────────────────┤
│                      PROCESSING LAYER                            │
│  Action Planner → Executor → create_*_direct / update_*          │
│  Entity linker (resolve BEFORE creation)                        │
│  Enrichment queue (Vercel-safe, queue-based)                     │
│  DLQ consumer │ State machine guards │ Compensate on fail        │
├──────────────────────────────────────────────────────────────────┤
│                      INTELLIGENCE LAYER                          │
│  Associative retrieval (7 signals + PPR)                        │
│  Knowledge graph (HITL for all edges, 5 types, 16 relations)    │
│  Context registry (6 strategies, entity-grounded gates)          │
│  Conversation threads (org + project + person scoped)            │
│  Cross-thread awareness layer                                   │
│  Brain synthesis / Pattern detection / Memory clustering         │
├──────────────────────────────────────────────────────────────────┤
│                      PRESENTATION LAYER                          │
│  Pulse Engine (single LLM call, write-behind)                   │
│  Decision Pulse (AI-free, pending approvals)                    │
│  Sentinel (meeting alarms + 7 piggyback services)               │
│  Rhodey's voice (character bible — pragmatic, loyal teammate)   │
├──────────────────────────────────────────────────────────────────┤
│                      PERSISTENCE LAYER                           │
│  16 formal state machines │ DB-backed state                     │
│  Temporal lineage (DB triggers)                                 │
│  pending_enrichment_jobs │ pending_retrieval_index_jobs          │
│  pending_graph_clarifications (survives cold restarts)           │
│  pending_nodes / merge_proposals (split from legacy table)       │
├──────────────────────────────────────────────────────────────────┤
│                      INTEGRATION LAYER                           │
│  Google Calendar/Tasks sync │ Telegram Bot API                   │
│  FCM Push notifications │ Gemini AI (3+ key rotation)           │
│  Upstash Redis cache │ Supabase (PostgREST)                     │
│  cron-job.org (sentinel + decision pulse)                       │
└──────────────────────────────────────────────────────────────────┘
```

### Key Principles

| Principle | Meaning |
|---|---|
| **Unified pipeline** | ALL input channels route through 1 pipeline: classify → url_filter → plan_actions |
| **DB-backed state** | Every session/user state lives in the database — survives Vercel cold restarts |
| **Queue-based enrichment** | No fire-and-forget async tasks. Every side-effect is a queue job with 3-retry dead-letter cycle |
| **Formal state machines** | All 16+ tables have documented valid transitions enforced by `guard_is_valid_transition()` |
| **Entity-grounded retrieval** | Every query is anchored to a real person, org, or project — prevents hallucination |
| **4W1H commits** | Every fix documents Root Cause, What, Where, When, How — enforced by git hook |

---

## Layer 1: Ingestion

### Channels

| Channel | Entry Point | Classification | Output |
|---|---|---|---|
| **Telegram** | `POST /api/webhook` | `classify.py` → intent + entity | `plan_actions()` |
| **WhatsApp** | `POST /api/whatsapp-ingest` | Flash Lite classify → actionable/fyi/ignored | Batched 3-min window → `plan_actions()` |
| **Email (Gmail)** | `email_ingest.py` (GHA cron) | `email_classify.py` → actionable/fyi | Draft approval flow → `plan_actions()` |
| **Email (Outlook)** | `email_ingest.py` (GHA cron) | Shared `build_email_classify_prompt()` | Same pipeline |
| **Teams** | `teams_ingest.py` (GHA cron) | Flash Lite classify | `plan_actions()` |
| **Calls** | `call_ingest.py` (GHA cron) | Gemini extraction | Decision Pulse approval |
| **Documents** | `multimodal.py` → `document_extractor.py` | Hybrid: PyMuPDF/docx/xlsx/pptx algorithmic + Gemini vision fallback | NOTE pipeline |

### Classifier Rules

| Rule | Intent | Receipt |
|---|---|---|
| Schedule questions ("meetings this week?") | `QUERY` | 🧠 Searching... |
| "Who is [name]?" | `QUERY` | 🧠 Searching... |
| Task creation ("buy groceries", "setup meeting") | `TASK` | ✅ Got it / 📋 Added / ✅ Created |
| Notes/ideas/MoMs | `NOTE` | 🧠 Captured / 📝 Noted |
| Task management ("close tasks", "cancel") | `COMPLETION` | ✅ Closed / ✅ Done 🎯 |
| Project updates | `PROJECT_UPDATE` | 🔄 Updated |
| Role attributions ("[name] is [role]") | `ROLE_UPDATE` | 👤 Updated |
| Daily briefing ("good morning", "what's my day?") | `DAILY_BRIEF` | 🧠 Briefing... |
| URLs | Intercepted at ingress → `resources` table (no LLM call) | N/A |

### URL Quarantine

Bare URLs are intercepted **before** the classifier. They skip all LLM processing and route directly to the `resources` table. This prevents URL-bearing text from entering the memory/graph extraction pipeline. Quarantine logic lives in `core/lib/url_filter.py` — single source of truth across all channels.

### Document Extraction

| Format | Engine | Speed | Cost | Quality |
|---|---|---|---|---|
| PDF | PyMuPDF | ~50ms | Free | 100% verbatim |
| DOCX | python-docx | ~10ms | Free | Paragraphs + tables |
| XLSX | openpyxl | ~20ms | Free | All sheets, flattened |
| PPTX | python-pptx | ~20ms | Free | All slides + shapes |
| Images | Gemini vision (SYNTHESIS_MODEL) | ~2s | Per-doc fee | Accurate OCR |
| Audio | Gemini audio transcription | ~3s | Per-doc fee | Verbatim |

Single entry point: `document_extractor.extract_text(file_bytes, mime_type)`.

### Key Files

| File | Purpose |
|---|---|
| `core/webhook/handler.py` | Telegram webhook entry, classify dispatch, command handling |
| `core/webhook/classify.py` | Flash Lite intent classification |
| `core/prompts/classify.py` | Classification prompt with all rules |
| `core/prompts/email_classify.py` | Shared email classification prompt |
| `core/lib/ingest.py` | Unified `ingest()` contract — single persist path |
| `core/lib/url_filter.py` | `check_and_quarantine_url()` — single URL handling source |
| `core/lib/document_extractor.py` | Hybrid document extraction (algorithmic + vision fallback) |
| `core/webhook/multimodal.py` | Multimodal file processing |
| `core/skills/email_ingest.py` | Gmail + Outlook fetch + classify |
| `core/skills/whatsapp_ingest.py` | WhatsApp notification ingestion + batching |
| `core/skills/call_ingest.py` | Call recording transcription + extraction |
| `core/skills/teams_ingest.py` | Microsoft Teams chat ingestion |

---

## Layer 2: Processing

### Action Planner — The Single Pipeline

All user intents route through a **unified Action Pipeline** — replacing the old 3-headed architecture (Webhook + Quick Process cron + Pulse staging sorter).

```
User message → classify() → plan_actions() → executor()
                                               ├── create_task_direct()
                                               ├── create_note_direct()
                                               └── update_task_status()
```

### Typed Action Model

```python
@dataclass
class Action:
    operation: Operation       # close_task, cancel_recurring, suppress_instance, modify_recurring,
                               # reschedule, update_metadata, delete_event
    target_id: Optional[id]    # Task ID, Calendar event ID, or None
    params: dict               # Operation-specific parameters
    confidence: float          # 0.0 - 1.0
    human_label: str           # Readable description
```

### Multi-Source Candidate Pool

The planner queries 3 data planes to resolve ambiguous commands:

1. **Active tasks**: `todo` / `in_progress` tasks
2. **Active recurring tasks**: Tasks with RRULE, even if `status='done'` (series still live)
3. **14-day calendar window**: Raw Google Calendar events (for events without task records)

### Entity Resolution (Before Creation)

Tasks and notes resolve their entity associations **before** creation, not after:

```
text → _resolve_project_and_org_id(text)
        ├── projects (name match)
        ├── organizations (name match)
        └── graph_nodes (people label match)
      → (project_id, organization_id, resolved_org_name)
```

### Enrichment Queue (Vercel-Safe)

Every side-effect (graph edges, entity extraction, embeddings) runs through a **queue-based** pattern:

```
create_task_direct()
  ├── INSERT task (synchronous, survives Vercel)
  └── INSERT pending_enrichment_job (synchronous)
        └── sentinel piggyback processes within ~5 min
              └── write_graph_edges_for_task()
              └── extract_and_link_entities()
              └── get_embedding()
```

| Before (broken) | After (safe) |
|---|---|
| `loop.create_task(enrich(...))` — killed by Vercel on return | `enqueue_enrichment(...)` — synchronous DB insert, survives cold kills |
| No retry — silent failure | 3-retry dead-letter lifecycle |
| No visibility | `pending_enrichment_jobs` table with status tracking |

### Multi-Intent Messages

Messages with multiple intents (e.g., "Cancel that and close the Amita tasks") are handled via:

1. **Primary intent** — Classified normally, routed through action planner
2. **secondary_actions** — Array of additional intents with confidence, processed after primary handler (threshold 0.5)
3. **_process_task_closure()** — Fuzzy-matches entity names against open task titles via substring/ILIKE

### DLQ Consumer

`core/skills/dlq_consumer.py` sweeps `audit_logs` (service='dlq') and retries with exponential backoff:
- 30s → 2min → 5min → Telegram alert

### Key Files

| File | Purpose |
|---|---|
| `core/actions/planner.py` | `plan_actions()` — single LLM resolution + candidate pool |
| `core/actions/executor.py` | Typed action execution with validation |
| `core/actions/models.py` | `Action`, `Operation` dataclass definitions |
| `core/pulse/tools.py` | `create_task_direct()`, `create_note_direct()`, `update_task_status()`, entity resolver |
| `core/lib/enrichment_queue.py` | Queue-based enrichment (enqueue + process + per-type processors) |
| `core/webhook/completion_handler.py` | Completion/closure execution |
| `core/webhook/workflows.py` | Batch workflow execution, task_closure in enrichment |
| `core/lib/state_machines.py` | `guard_is_valid_transition()` for all tables |
| `core/skills/dlq_consumer.py` | Dead letter queue consumer with exponential backoff |

---

## Layer 3: Intelligence

### Associative Retrieval (7-Signal Ranking)

Replaces legacy pgvector-only search with a 7-signal pipeline:

```
Query → LLM entity extraction (parallel) + lexical n-grams (parallel)
  → SHA-256 Redis cache check (warm path ≈1.8s, cold path ≈5s)
  → 7 signals blended with configurable weights:
      1. Semantic (embedding cosine similarity)
      2. PPR (personalized PageRank on phrase graph)
      3. Recency (time decay)
      4. Importance (memory score)
      5. Project boost (project-context penalty)
      6. Specificity (node degree — rare terms rank higher)
      7. Person boost (person mention boost)
  → Ranked memories with scores
```

| Metric | Before (pgvector) | After (associative) |
|---|---|---|
| Cold path | ~23s | ~3.5–5.0s |
| Warm path (cached) | ~9s | ~1.8–3.5s |
| Coverage | Vector only | Vector + graph + recency + importance |

### Knowledge Graph

| Dimension | Details |
|---|---|
| **Node types** | 5: person, organization, project, place, animal |
| **Edge types** | 16: DISCUSSED_WITH, MET_WITH, INTRODUCED, FRIEND_OF, PARENT_OF, SPOUSE_OF, SIBLING_OF, FAMILY_OF, PET_OF, MENTORS, WORKS_AT, WORKS_ON, CLIENT_OF, VENDOR_TO, MEMBER_OF, SERVES_AT |
| **Edge flow** | ALL edges flow through `pending_graph_edges` → HITL approval → `graph_edges` |
| **Validation** | `VALID_EDGE_MATRIX` — positive allowlist mapping allowed (source_type, target_type, relationship) triples |
| **Guard A** | Deletes stale BELONGS_TO edges before inserting new ones |
| **Guard B** | Text-anchoring — rejects entities not present verbatim in source text |
| **Guard C** | HITL for ALL new person/org/project nodes |

### Context Registry

6 strategies for retrieving context, each with configurable thresholds, fact sources, and gate modes:

| Strategy | When Used | Sources | Gate |
|---|---|---|---|
| **PRE_FLIGHT** | Before sentinel briefing | tasks, calendar, canonical | Hard (requires entity anchor) |
| **BRIEFING** | Pulse Engine briefing | tasks, memories, projects, calendar | Soft |
| **HINDSIGHT** | Memory/pattern analysis | memories, tasks | Soft |
| **HYDRATE_TASKS** | Context hydration for tasks | tasks, projects, graph | Soft |
| **HYDRATE_MEMORIES** | Context hydration for memories | memories, graph | Soft |
| **BRAIN_SYNTH** | Brain synthesis pipeline | memories, canonical, graph | None |

### Conversation Threads

| Feature | Details |
|---|---|
| **Thread types** | general (default), entity (org/project/person with scoped context), workflow (active batch operations) |
| **Routing priority** | 1. Open workflow → 2. Exact entity match → 3. Prior bot question → 4. General fallback |
| **Entity scoring** | Projects (90) > Orgs (80) > People (75) — "Anita" → person thread; "Marcus from Ashraya" → org thread |
| **Person routing** | `_resolve_person_candidates()` via graph_nodes type='person', n-gram primary-topic detection |
| **Summaries** | Eager — generated every 3rd user exchange via Flash Lite, always updated |
| **Embeddings** | ALL exchange types embedded fire-and-forget (not just QUERY) |
| **Awareness layer** | Parallel Phase 2 task scans 24h threads for entity cross-references, injects `ACTIVE CONVERSATION CONTEXT` |
| **Auto-archive** | Entity threads >7 days inactive → archived by sentinel piggyback (50/cycle, excludes general thread) |

### Brain Synthesis

Nightly cron generates **canonical pages** — holistic organization-level summaries synthesized from all underlying projects, memories, tasks, and graph edges. Uses associative retrieval for content assembly.

### Key Files

| File | Purpose |
|---|---|
| `core/retrieval/search.py` | `associative_retrieve()` — 7-signal ranking engine |
| `core/retrieval/graph.py` | `build_triple_graph()` — batch phrase node operations |
| `core/retrieval/ranking.py` | `rank_memories()` — 7-signal blend with configurable weights |
| `core/retrieval/ppr.py` | `personalized_pagerank()` — ~50ms on <2000 node subgraphs |
| `core/retrieval/pipeline.py` | `index_memory()` / `schedule_index_memory()` — forward indexing |
| `core/retrieval/config.py` | Feature flags + `RETRIEVAL_INDEXING_ENABLED` |
| `core/retrieval/eval.py` | Side-by-side pgvector vs associative comparison |
| `core/context/pipeline.py` | Context registry — entity-grounded retrieval orchestration |
| `core/context/config.py` | 6 strategy profiles (thresholds, sources, gate modes) |
| `core/context/gates.py` | Hard/soft/None entity grounding gates |
| `core/lib/conversation.py` | Thread routing, summaries, embeddings, awareness |
| `core/pulse/entity_extractor.py` | Real-time entity extraction → pending tables |
| `core/pulse/graph.py` | Graph operations — node creation, edge approval, HITL |
| `core/pulse/memory.py` | Memory retrieval, enrichment, expiry |
| `core/pulse/context.py` | Context hydration (tasks, calendar, canonical) |
| `core/skills/brain_synth_v2.py` | Canonical page generation |
| `core/lib/graph_rules.py` | `VALID_EDGE_MATRIX`, label/normalization helpers |
| `core/pulse/clarifier.py` | Clarifier Phase 2 — 85%+ auto-merge, edge contradiction detection |

---

## Layer 4: Presentation

### Pulse Engine

| Component | What it does |
|---|---|
| `core/pulse/briefing.py` | Single LLM call (gemini-3.5-flash), write-behind pattern. 2-phase parallelized: Phase 1 (independent queries) → Phase 2 (cross-referenced context). ~30-40% faster than legacy |
| `core/pulse/decision_pulse.py` | AI-free — pending approvals only. Queries messages + pending_nodes + pending_graph_edges |
| `core/pulse/sentinel.py` | 5-min cron. Meeting alarms (60-min lookahead) + 7 piggyback services (index queue, enrichment queue, auto-archive, pattern detection, feedback ingestion, ND sweep, enrichment processing) |
| `core/pulse/pipeline.py` | Consolidated health monitor — DLQ items, recent errors, LLM degradation checks |
| `core/pulse/run_logger.py` | Pulse run tracking |
| `core/pulse/models.py` | Clean data contracts (dead fields removed) |
| `core/pulse/engine.py` | *(Legacy — staged removal complete. Parts 51-57)* |

### Sync Schedule (cron-job.org)

| Job | URL | Frequency |
|---|---|---|
| **Sentinel Nudge** | `POST /api/sentinel` | Every 5 min |
| **Decision Pulse** | `POST /api/decision-pulse` | Every 30 min |
| **Evening Roundup** | `POST /api/roundup` | 2PM, 8PM IST |

### Rhodey's Voice (Character Bible)

Defined in `core/prompts/voice.py`:
- **Who**: Pragmatic, loyal teammate. Not "chief of staff" or "strategic partner."
- **How**: Direct, punchy, varied phrasing. No self-narration.
- **Words banned**: "operational", "strategic", "leverage", "utilize"
- **Tone varies**: Work = concise + professional. Personal = warm. Faith = measured reverence.

### Streaming Responses

Queries stream Gemini responses for faster time-to-first-token:
- Uses `Telegram.sendMessage` for each chunk
- `editMessageText` for updates
- Two prompt paths: streaming (natural text) vs non-streaming (JSON wrapper)

### Logical Gap Fixes (G1-G10)

| Gap | Fix |
|---|---|
| G1: Urgent tasks on weekends | Weekend filter BEFORE urgency check |
| G2: Ideas/resources on weekends | Stripped from weekend briefs |
| G3: Completed shown as current | CONTEXT_SECTION_RULES annotation |
| G4: Cache not invalidated on auto-expire | `caches['tasks'].invalidate()` added |
| G5: Cache not invalidated on Google sync | Same invalidation pattern |
| G6: COMPASS LENS vs MODE OVERRIDES | COMPASS LENS removed |
| G10: Clarification question ignored | Now uses `classification.clarification_question` |

### Key Files

| File | Purpose |
|---|---|
| `core/pulse/briefing.py` | AI briefing generation (single LLM, parallelized) |
| `core/pulse/decision_pulse.py` | AI-free pending approvals |
| `core/pulse/sentinel.py` | Meeting alarms, piggyback services, auto-archive |
| `core/pulse/pipeline.py` | Consolidated health check (DLQ, errors, LLM degradation) |
| `core/pulse/run_logger.py` | Pulse run tracking |
| `core/prompts/voice.py` | Character bible — pragmatic, loyal teammate tone |
| `core/prompts/query.py` | Streaming/non-streaming paths, CONTEXT_SECTION_RULES |
| `core/prompts/briefing.py` | Briefing prompt (COMPASS LENS removed) |
| `core/prompts/classify.py` | Classify prompt with natural receipt variations |
| `core/prompts/planner.py` | Action Planner prompt (extracted from inline) |

---

## Layer 5: Persistence

### State Machine Governance

Every table has documented valid status transitions in `core/lib/state_machines.py` (468 lines). All transitions pass through `guard_is_valid_transition(table, from_status, to_status)` before execution.

**16 tables covered:** raw_dumps, tasks, memories, messages, pending_nodes, merge_proposals, pending_graph_edges, graph_nodes, graph_edges, conversations, conversation_threads, decisions, email_drafts, pending_retrieval_index_jobs, pending_graph_clarifications, agent_queue, call_recordings, retrieval_index_runs.

### DB-Backed State (Survives Vercel Cold Kills)

| What | Table | Type |
|---|---|---|
| Clarification dialogs | `pending_graph_clarifications` | Session state |
| Active sessions | `pending_graph_clarifications` (type='session') | Session state |
| Enrichment jobs | `pending_enrichment_jobs` | Queue |
| Index jobs | `pending_retrieval_index_jobs` | Queue |
| Node approvals | `pending_nodes` | HITL |
| Merge proposals | `merge_proposals` | HITL |
| Edge approvals | `pending_graph_edges` | HITL |
| Webhook jobs | `pending_webhook_jobs` | Queue |
| Memory timeline | `memories` (is_current, version, supersedes_id) | Temporal lineage |
| Task timeline | `tasks` (DB trigger `trg_temporal_task_update`) | Temporal lineage |

### Key Schema Patterns

| Pattern | Example |
|---|---|
| **Temporal lineage** | `is_current BOOLEAN`, `version INTEGER`, `supersedes_id UUID` on tasks, memories, graph_nodes, graph_edges, projects, people, canonical_pages |
| **DB triggers** | `trg_temporal_task_update` — BEFORE UPDATE archives old row, increments version, preserves primary key |
| **Normalized dedup** | `normalized_label TEXT UNIQUE` on graph_nodes — PostgREST-compatible case-insensitive dedup |
| **Unique entity threads** | `idx_unique_active_entity_thread` on `conversation_threads(chat_id, thread_type, entity_type, entity_id)` WHERE archived_at IS NULL |
| **Atomic claims** | `claim_pending_enrichment_job()` RPC with `pg_advisory_xact_lock` — prevents double-processing |

### Key Files

| File | Purpose |
|---|---|
| `core/lib/state_machines.py` | Formal state machines (468 lines, 16 tables) |
| `core/lib/node_tables.py` | pending_nodes / merge_proposals abstraction |
| `core/lib/clarification_state.py` | DB-backed clarification state (get/set/resolve) |
| `core/services/db.py` | Supabase client, zombie_recovery(), versioning helpers |
| `db/` | Migration scripts (42+ files, numbered sequentially) |

---

## Layer 6: Integration

### External Services

| Service | Integration | Auth |
|---|---|---|
| **Gemini AI** | LLM calls via `core/llm/client.py` — 3-key rotation on 429 | `GEMINI_API_KEY[1-3]` |
| **Supabase** | PostgREST API via `core/services/db.py` | `SUPABASE_SERVICE_ROLE_KEY` |
| **Google Calendar** | Event CRUD via `core/services/google_service.py` | OAuth2 refresh token |
| **Google Tasks** | Task sync | Same OAuth2 |
| **Telegram** | Bot API — sendMessage, editMessageText, answerCallbackQuery | `TELEGRAM_BOT_TOKEN` |
| **FCM Push** | `core/services/push_notification.py` — fire-and-forget on every send | `device_tokens` table |
| **Upstash Redis** | `core/lib/redis_cache.py` — caching for retrieval pipeline (1h/24h TTL) | `UPSTASH_REDIS_REST_URL/TOKEN` |
| **cron-job.org** | Sentinel (5min), Decision Pulse (30min), Evening Roundup (2PM/8PM) | `x-pulse-secret` header |
| **Notebook LM** | Google Docs API — auto-sync canonical pages | OAuth2 (docs scope) |

### LLM Architecture

```
                ┌──────────────────┐
                │  get_gemini_clients() │
                │  (3 API keys)     │
                └────────┬─────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
  ┌─────────────┐ ┌──────────┐ ┌──────────┐
  │ SYNTHESIS   │ │CLASSIFI- │ │EMBEDDING │
  │ MODEL       │ │CATION    │ │MODEL     │
  │ (Flash 3.5) │ │MODEL     │ │(gemini-  │
  │             │ │(Flash    │ │embedding-│
  │             │ │Lite 3.1) │ │2-preview)│
  └─────────────┘ └──────────┘ └──────────┘
       │               │              │
       ▼               ▼              ▼
  ┌──────────────────────────────────────────┐
  │        Fallback chain                    │
  │  generate_content_with_fallback()        │
  │  → tries each key on 429                 │
  │  → jittered exponential backoff          │
  └──────────────────────────────────────────┘
```

### Google Connectivity

```python
# Single entry point for all Google services
creds = get_google_creds()  # OAuth2 refresh token flow
service = get_service('calendar', 'v3', creds)
tasks_service = get_tasks_service(creds)
docs_service = get_docs_service(creds)  # For Notebook LM sync
```

### Key Files

| File | Purpose |
|---|---|
| `core/llm/client.py` | `get_gemini_client()`, `get_gemini_clients()` (multi-key rotation) |
| `core/llm/fallback.py` | `generate_content_with_fallback()` — key rotation + retry |
| `core/llm/embedding.py` | `get_embedding()` — multi-key failover on 429 |
| `core/llm/constants.py` | Model constants, retry/non-retry error lists |
| `core/services/google_service.py` | Google Calendar/Tasks/Docs API, `format_rfc3339()` |
| `core/services/push_notification.py` | FCM push (fire-and-forget) |
| `core/services/db.py` | `get_supabase()` — singleton client |
| `core/lib/redis_cache.py` | `cache_get()` / `cache_set()` — Redis via Upstash |

---

## Key Metrics

| Metric | Value |
|---|---|
| **Average response time** | ~34s (across 15 verified queries) |
| **Fastest query** | 25s (schedule queries) |
| **Slowest query** | 50s (general — all 17 sections loaded) |
| **Timeout rate** | 0% (15/15 under 60s Vercel limit) |
| **Hallucination rate** | 0% verified |
| **E2E tests** | 22 scenarios, all passing |
| **State machines** | 16 tables formally documented |
| **Dead code removed** | ~700+ lines across 7 files |
| **Test artifacts cleaned** | ~1,094 rows across 21 tables |
| **Graph dedup** | 1,235 task nodes → 170 unique |
| **Indexed passages** | 704/704 enriched passages with links |

---

## Changelog

| Date | Part | Summary |
|---|---|---|
| Jul 9 | 20 | Topic overlap guard, graph write consolidation, `normalized_label` fix |
| Jul 10-11 | 27 | Rhodey Surface v3, FCM push, diagnostic endpoints |
| Jul 12 | 22 | Graph cross-domain linkages, 4-layer WORKS_AT extraction |
| Jul 13 | 28 | Notebook LM auto-sync, temporal versioning migration |
| Jul 14 | 50 | Multi-intent messages, task closure pipeline |
| Jul 15 | 51-55 | Universal Action Planner, holistic architecture, DB-backed state, trigger crash fix, 4W1H enforcement |
| Jul 16 | 56-57 | Enrichment queue, architecture cleanup (`now_ist()`, health consolidation), dead file removal |
| Jul 17 | 58-59 | 22-scenario UAT validation, post-UAT cleanup (~1,094 rows) |
| Jul 18-19 | 60 | Hybrid document extraction (PyMuPDF, docx, xlsx, pptx) |
| Jul 19-20 | 61 | Parallelization, streaming, voice overhaul, G1-G10 gap fixes, 15-query UAT |
| Jul 20-21 | 62 | Hardened thread layer — person routing, eager summaries, all-exchange embeddings, cross-thread awareness, auto-archive |
