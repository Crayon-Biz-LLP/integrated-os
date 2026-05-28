# 5. Database Schema

## Overview

The system uses Supabase (PostgreSQL 15 + pgvector) with 21 tables, 27 indexes, 5 RPC functions, and 4 explicit foreign key constraints. All migrations are versioned in `/migrations/` (17 SQL files).

## Schema Map

### Core Tables

| Table | Rows Est. | Primary Key | Purpose |
|-------|-----------|-------------|---------|
| `tasks` | High | `id` (BIGSERIAL) | All tasks with versioning, project FK, Google IDs |
| `raw_dumps` | Very High | `id` (BIGSERIAL) | Raw message intake pipeline |
| `memories` | Very High | `id` (BIGSERIAL) | Vector-embedded knowledge with versioning |
| `graph_nodes` | Medium | `id` (BIGSERIAL) | Knowledge graph vertices (5 types) |
| `graph_edges` | High | `id` (BIGSERIAL) | Knowledge graph edges (3 relationship types) |

### Business Tables

| Table | Purpose |
|-------|---------|
| `projects` | Active projects with org tags, keywords, parent, versioning |
| `resources` | URLs with AI enrichment, mission linking |
| `missions` | Strategic goals auto-created by Pulse or declared via Telegram |
| `people` | Network contacts with strategic weight, source tracking |
| `core_config` | Key-value config store (season, heartbeat, entity mappings) |

### Email Tables

| Table | Purpose |
|-------|---------|
| `emails` | Ingested emails (Gmail + Outlook) with classification, linked entities |
| `email_pending_tasks` | AI-suggested tasks awaiting human approval (with duplicate guard) |
| `email_drafts` | AI-generated email draft replies pending send |

### Infrastructure Tables

| Table | Purpose |
|-------|---------|
| `failed_queue` | Dead letter queue for retries (embedding, memory insert) |
| `audit_logs` | Every system operation logged (service, level, metadata) |
| `model_registry` | Every LLM call tracked (model, provider, tokens, latency, success) |
| `processed_updates` | Telegram dedup (update_id uniqueness) |
| `conversations` | Chat history with intent, session management |
| `canonical_pages` | AI-synthesized master pages (versioned, never overwritten) |
| `agent_queue` | Pending research/delegate tasks |
| `logs` | Pulse AI JSON log entries |

## Critical Table Details

### `tasks` (15 columns)

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL | PK |
| `title` | TEXT | NOT NULL |
| `project_id` | BIGINT | FK → projects(id) ON DELETE SET NULL |
| `priority` | TEXT | Default 'important' |
| `status` | TEXT | Default 'todo' |
| `estimated_minutes` | INTEGER | Default 15 |
| `duration_mins` | INTEGER | Default 15 |
| `reminder_at` | TIMESTAMPTZ | For calendar sync |
| `dedup_key` | TEXT | MD5 hash of title+project_id |
| `google_task_id` | TEXT | Google Tasks sync ID |
| `google_event_id` | TEXT | Google Calendar event ID |
| `is_revenue_critical` | BOOLEAN | Default FALSE |
| `completed_at` | TIMESTAMPTZ | Set on done |
| `is_current` | BOOLEAN | Default TRUE (versioning) |
| `version` | INTEGER | Default 1 (versioning) |
| `supersedes_id` | BIGINT | Points to previous version |

### `memories` (18 columns)

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL | PK |
| `content` | TEXT | NOT NULL |
| `memory_type` | TEXT | note / outcome / archive / reflection / relationship_note |
| `embedding` | VECTOR(768) | Gemini embedding for semantic search |
| `embedding_status` | TEXT | pending / success / failed |
| `source` | TEXT | webhook / pulse_outcome / pulse_note / email_ingest / archive_ingest |
| `metadata` | JSONB | Flexible metadata per type |
| `project_id` | BIGINT | FK → projects(id) |
| `is_current` | BOOLEAN | Versioning support |
| `version` | INTEGER | Versioning support |
| `supersedes_id` | BIGINT | Version chain |
| `importance_score` | INTEGER | CHECK 1-10, default 5 |
| `is_archived` | BOOLEAN | Pruning support |
| `pruned` | BOOLEAN | Garbage collection |

### `graph_nodes` (4 columns but high-value metadata)

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL | PK |
| `label` | TEXT | Display name |
| `type` | TEXT | task / project / person / practice / mission |
| `metadata` | JSONB | Source, relationships, health_score, all entity-specific data |

### `graph_edges` (6 columns)

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL | PK |
| `source_node_id` | BIGINT | FK → graph_nodes(id) ON DELETE CASCADE |
| `target_node_id` | BIGINT | FK → graph_nodes(id) ON DELETE CASCADE |
| `relationship` | TEXT | BELONGS_TO / INVOLVES / DEPENDS_ON / PRECEDES / FOLLOWED_BY |
| `weight` | FLOAT | Default 1.0 |
| `metadata` | JSONB | Source, task_id, confidence, matched_name |

## RPC Functions

| Function | Returns | Purpose |
|----------|---------|---------|
| `match_memories(jsonb, float, int)` | Table | Vector similarity search (cosine distance) |
| `match_canonical_pages(jsonb, float, int)` | Table | Vector search on canonical pages |
| `get_memory_at_time(bigint, timestamptz)` | Table | Time-travel: what did this memory look like at time T? |
| `detect_drift(text, int)` | Table | How many times was this project updated in N hours? |
| `prune_old_memories()` | Integer | Garbage collect low-importance, old memories |

## Index Strategy

27 indexes across all tables, strategically designed:
- Partial indexes on `is_current = TRUE` for versioned tables (fast active-record queries)
- GIN index on `graph_nodes(metadata)` for JSONB queries by practice status, entity type
- Composite indexes on `(direction, created_at DESC)` and `(sender, created_at DESC)` for message history queries
- Conditional indexes on `embedding_status != 'success'` for monitoring failed embeddings

## Foreign Key Relationships

| From | To | On Delete |
|------|----|-----------|
| `tasks.project_id` | `projects.id` | SET NULL |
| `memories.project_id` | `projects.id` | SET NULL |
| `graph_edges.source_node_id` | `graph_nodes.id` | CASCADE |
| `graph_edges.target_node_id` | `graph_nodes.id` | CASCADE |

(Implicit: `email_pending_tasks.email_id` → `emails.id`, `email_drafts.email_id` → `emails.id`, `resources.mission_id` → `missions.id`)
