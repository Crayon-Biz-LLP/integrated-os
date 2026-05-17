# Rhodey OS — Architecture Plan
> Technical decisions, stack rationale, and system design constraints.

---

## Stack

| Layer | Technology | Reason |
|---|---|---|
| **Capture / Webhook** | Python (FastAPI via GitHub Actions) | Existing, working |
| **Database** | Supabase (Postgres + pgvector) | Existing. Vector search + relational in one place |
| **Embedding** | Gemini `gemini-embedding-2-preview`, 768 dims | Existing. Do not change without full re-embed |
| **Classification** | Gemini 1.5 Flash | Cost-efficient for high-frequency classification |
| **Email** | Gmail API + Microsoft Graph API | Existing. Both OAuth2 token-refresh flows |
| **Briefing / Brain** | Gemini 1.5 Pro | Reserved for synthesis tasks only |
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


[GitHub Actions Cron]
  ├── core/pulse/engine.py (daily briefing)
  ├── brain_synth.py (weekly synthesis)
  ├── email_ingest.yml (Gmail + Outlook)
  └── janitor.py (every 30 mins health check)
```

---

## Key Design Decisions

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

