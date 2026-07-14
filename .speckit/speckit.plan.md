# Rhodey OS — Architecture Plan
> Technical decisions, stack rationale, and system design constraints.

---

## Stack

| Layer | Technology | Reason |
|---|---|---|
| **Capture / Webhook** | Python (FastAPI via GitHub Actions) | Existing, working |
| **Database** | Supabase (Postgres + pgvector) | Existing. Vector search + relational in one place |
| **Embedding** | Gemini `gemini-embedding-2-preview`, 768 dims | Existing. Do not change without full re-embed |
| **Classification** | Gemini 3.1 Flash Lite | Cost-efficient for high-frequency classification |
| **Email** | Gmail API + Microsoft Graph API | Existing. Both OAuth2 token-refresh flows |
| **Briefing / Brain** | Gemini 3.5 Flash | Reserved for synthesis tasks only |
| **Retrieval (semantic)** | Gemini `gemini-embedding-2-preview`, 768 dims | Query embedding, passage embedding, phrase node embedding |
| **Retrieval (lexical)** | GIN trigram indexes + `retrieval_phrase_nodes` | ~5ms phrase lookups via `pg_trgm` |
| **Retrieval (graph)** | Personalized PageRank on subgraph | ~50ms on bounded subgraph (<2000 nodes) |
| **Retrieval (cache)** | Upstash Redis (SHA-256 keys) | LLM extraction (1h TTL), embeddings (24h TTL) |
| **Scheduling** | GitHub Actions (cron) | No separate infra. Acceptable latency for background jobs |
| **Alerting** | Telegram Bot API | Danny lives in Telegram. Zero latency to operator |
| **Graph** | Supabase `graph_nodes` + `graph_edges` tables | Lightweight — no external graph DB needed at current scale |

---

## Architecture Diagram (Text)

```
[Danny: Telegram]
       │
       ▼
[core/webhook/handler.py] ── classify ──► [Gemini Flash]
       │
       ├──► TASK ──────────────► [raw_dumps: staged]
       │                               │
       ├──► NOTE ──────────────►       │
       │                               ▼
       ├──► QUERY ────────────► [interrogate_brain()]
       │                               │
       └──► NOISE ───────────► [log to audit, discard]
                                        │
                                [Background Processor]
                                        │
                             ┌──────────┴──────────┐
                             ▼                     ▼
                      [get_embedding()]      [fail → DLQ]
                             │
                      [memories insert]
                             │
                      [raw_dumps: processed]
                             │
                             ▼
              ┌──────────────────────────────┐
              │  Associative Retrieval Index │
              │  (schedule_index_memory)     │
              │                              │
              │  chunk → embed → extract     │
              │  → upsert phrase nodes       │
              │  → link passages → bundle    │
              └──────────┬───────────────────┘
                         │
              ┌──────────▼───────────┐
              │  7 tables: passages  │
              │  phrase_nodes, stats │
              │  links, alias_edges  │
              └──────────────────────┘

              ┌──────────────────────────────┐
              │  Associative Retrieval Query │
              │  (associative_retrieve)      │
              │                              │
              │  LLM entities + lexical      │
              │  → PPR graph traversal       │
              │  → 7-signal ranking          │
              │  → ExplainableBundle         │
              └──────────────────────────────┘


[GitHub Actions Cron]
  ├── core/pulse/engine.py (daily briefing)
  ├── brain_synth.py (weekly synthesis)
  ├── email_ingest.yml (Gmail + Outlook)
  ├── janitor.py (every 30 mins health check)
  └── notebooklm-sync.yml (on push: Google Docs → Notebook LM)

[Cron-job.org (external)]
  ├── /api/sentinel (every 5 min — nudge, expiry, index queue)
  ├── /api/decision-pulse (every 30 min — pending approvals)
  └── /api/roundup (2PM/8PM IST — evening check-in)

[Flutter App — rhodey_app/]
  ├── FCM push notifications from send_telegram()
  ├── In-app update via GitHub Releases (version from pubspec.yaml)
  ├── Rhodey Surface v3: Horizon/Traces UI (editorial typography, warm stone palette)
  ├── TTS for Rhodey responses
  ├── Voice mic button for speech input
  ├── today_screen: task/trace/conversation search
  ├── talk_screen: voice + TTS interaction
  ├── adaptive_home_screen, dump_screen, history_screen, inbox_screen, menu_sheet
  ├── chat_bubble, decision_card, rich_card_content, voice_states widgets
  └── API service, notification service, in-app update service

[process_single_dump Refactoring — core/lib/process_input.py]
  ├── Extracted core processing logic from dispatch.py into a shared module
  ├── Calendar event creation funneled through existing task workflow
  └── New test suite: tests/sim/test_full_pipeline.py

[Notebook LM Sync — Google Docs API]
  ├── scripts/sync_notebooklm_docs.py — creates/updates Google Docs in shared Drive
  ├── scripts/update_google_oauth.py — one-time OAuth scope updater
  └── .github/workflows/notebooklm-sync.yml — triggers on push to main
```

---

## Key Design Decisions

### Decision 0: Temporal Lineage via PostgreSQL triggers (not application-level)
**Chosen**: BEFORE UPDATE triggers on `tasks` and `canonical_pages` tables automatically archive old state and increment versions. `pg_trigger_depth() = 0` guard prevents cascading trigger re-entry.
**Rejected**: Application-level versioning in Python via `create_versioned_task()`.
**Why**: Application-level versioning requires every code path (Python webhook, Next.js API routes, direct SQL) to remember to call the versioning function. A database trigger catches ALL update paths transparently without changing existing `supabase.table('tasks').update()` calls. The trigger preserves the primary key (only inserts a new historical row, doesn't change the active row's ID), preventing Google Calendar sync mappings from breaking.
**Tradeoff**: Slightly more complex DB migration. But triggers are well-understood PostgreSQL patterns with zero application overhead.

### Decision 1: No real-time embedding in the webhook response path
**Chosen**: Stage the record immediately, embed asynchronously.
**Rejected**: Embedding inline during webhook response.
**Why**: Gemini embedding API at ~1-2s latency causes Telegram webhook timeouts. The user gets an immediate `✅ Captured` receipt. Memory is indexed within 5 minutes.

### Decision 2: GitHub Actions as background job runner
**Chosen**: Schedule Pulse, Janitor, Synth as GitHub Actions crons.
**Rejected**: A persistent worker (Railway, Render, Celery).
**Why**: Zero infra cost. Danny's system runs ~10-20 inputs per day — no need for a persistent worker. Cold start latency (30-60 seconds) is acceptable for all background jobs.

### Decision 3: Supabase as single data store (no separate vector DB)
**Chosen**: `pgvector` extension in Supabase.
**Rejected**: Pinecone, Weaviate, Qdrant.
**Why**: At Danny's data scale (< 50,000 memories), pgvector outperforms managed vector DBs on latency AND eliminates a sync layer. Revisit at 500K+ records.

### Decision 4: Hybrid Graph + Vector search
**Chosen**: Entity graph for structural context + vector search for semantic similarity.
**Rejected**: Vector-only RAG.
**Why**: "What did I think about Solvstrat?" needs vector. "What are all people connected to Solvstrat?" needs graph. Combining both gives Danny interrogation that a pure RAG system cannot.

### Decision 5: Entity extraction is best-effort + async
**Chosen**: Extract entities and write graph edges in the background after the webhook response returns.
**Rejected**: Inline LLM extraction during webhook response.
**Why**: The webhook must respond to Telegram within a few seconds. Background processing handles LLM latency gracefully.

### Decision 6: Human-in-the-loop for ALL graph entities AND edges
**Chosen**: Every new entity requires HITL approval — 5 core types (person, organization, project, place, animal). All extracted edges (16 types) flow through `pending_graph_edges` for approval. No auto-create for any node type.
**Rejected**: Auto-create all extracted entities; concept nodes (EVOKES, RELATES_TO, ASSOCIATED_WITH) — concept system fully removed in Phase 20, purged 997 nodes + 678 edges from DB.
**Why**: The graph is the backbone of Rhodey's intelligence — wrong nodes infect every query and brief. The "low-risk auto-create" approach created 699 junk nodes. Every edge is now staged in `pending_graph_edges` with inline editing in the Decisions UI before approval. `pending_graph_nodes` and `pending_graph_edges` both have RLS enabled for extra safety. Concept nodes were fully removed — emotions live on memory metadata, abstract concepts are not tracked in the graph.
**Tradeoff**: Latency between extraction and graph availability — but the Decisions UI Graph Edges tab makes batch approval fast.

### Decision 7: Associative Retrieval over pgvector-only
**Chosen**: 7-signal associative retrieval with dedicated retrieval tables (passages, phrase nodes, alias edges, etc.).
**Rejected**: Keeping `match_memories_hybrid` as the sole retrieval path.
**Why**: pgvector-only search misses multi-word phrases, doesn't leverage graph structure, and has no alias/entity bridging. The new system: (1) caches LLM entity extraction and embeddings in Redis (warming ~3.5s → ~10ms), (2) uses GIN trigram indexes for ~5ms lexical phrase matching, (3) runs PPR graph traversal for indirect connections (~50ms), (4) blends 7 ranking signals with configurable weights. Cold path: 3.5–5.0s, warm path: 1.8–3.5s — down from ~9s on legacy pgvector.
**Tradeoff**: 7 new database tables + 2 Redis caching tiers = operational surface area. But all caching is fail-open, and the legacy path remains as fallback via `RETRIEVAL_SHADOW_MODE=false`.

---

## Scale Assumptions

- ~20 inputs/day from Telegram
- ~50 emails/day ingested
- ~500 active memories at any point
- Peak load: never more than 5 concurrent webhook events
- No multi-user. Single operator always.

---

## What Must NOT Change Without Constitution Review

1. The embedding model (requires full re-embed of `memories`)
2. The `entity` routing rules (SOLVSTRAT, CRAYON, etc.)
3. The `status` enum on `raw_dumps` (requires migration)
4. The Telegram bot token (requires update to all webhook registrations)
5. The Supabase project URL or anon key (requires update to all env secrets in GitHub)
6. The `normalized_label` column on `graph_nodes` (migration already applied; affects all graph node upserts)
7. The `CONVERSATION THREAD SUMMARY:` / `PRECEDING TURN` structure in classification context (bounded classify context blocks are the sole interface for the classify prompt)

