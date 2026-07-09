# 5. Database Schema (Supabase / PostgreSQL)

## Overview

- **Tables:** 20 tables: `tasks`, `raw_dumps`, `memories`, `graph_nodes`, `graph_edges`, `projects`, `resources`, `clusters`, `people`, `core_config`, `messages`, `pending_graph_nodes`, `pending_graph_edges`, `system_audit_logs`, `dead_letter_queue`, `pulse_diagnostics`, `scheduled_tasks`, `suppressed_tasks`, `user_settings`.
- **Auth:** Service role key with RLS disabled (server-side); RLS enabled on `pending_graph_edges`, `pending_graph_nodes`, `messages`, `system_audit_logs`, `dead_letter_queue` (bypassed by service role).
- **Partitioning:** `raw_dumps` has a daily partition trigger, created at runtime in `db.py:create_partition_if_needed()`.
- **Embeddings:** Only the `memories` table stores vector embeddings.
- **Indexes:** GIN index on `memories.embedding` for vector search, B-tree on `created_at` for time-range queries, composite index on `memories (type, created_at)`.

## Core Tables

### `tasks`

| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | |
| title | TEXT | Task description |
| status | TEXT | `pending`, `done`, `cancelled`, `superseded` |
| priority | TEXT | `critical`, `high`, `medium`, `low` |
| project_id | TEXT | FK → `projects.id` |
| due_date | TIMESTAMPTZ | |
| source | TEXT | `pulse`, `telegram`, `quick_process` |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |
| raw_dump_id | BIGINT | FK → `raw_dumps.id` |
| raw_dump_source | TEXT | |
| organization_name | TEXT | `SOLVSTRAT`, `QHORD`, `ASHRAYA`, `PERSONAL`, `CRAYON` |
| goal_id | UUID | FK → `goals.id` |
| is_bad | BOOLEAN | Noise flag |
| direction | TEXT | `inbound`, `outbound`, `waiting_on` — who owns the action |
| committed_to | TEXT | Person name — who this task is committed to/for |
| committed_on | TIMESTAMPTZ | When the commitment was made |

Added: `direction` (inbound/outbound/waiting_on), `committed_to` (person name), `committed_on` (timestamp). These fields let the Pulse surface commitment bottlenecks: "waiting_on Marcus for Equisoft approval" instead of silently rotting.

### `raw_dumps`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| user_input | TEXT | Original unprocessed text |
| chat_id | BIGINT | |
| message_id | INT | |
| created_at | TIMESTAMPTZ | |
| processed | BOOLEAN | Quick processing flag |
| source | TEXT | |

### `memories`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| content | TEXT | Raw memory content |
| type | TEXT | One of 6 types (see Memory System doc) |
| embedding | VECTOR(768) | Gemini embedding-2 |
| importance_score | INTEGER | 1-10 |
| raw_dump_id | BIGINT | FK → `raw_dumps.id` (nullable) |
| created_at | TIMESTAMPTZ | |
| project_id | TEXT | FK → `projects.id` |
| organization_name | TEXT | |
| source_table | TEXT | `memories` or `raw_dumps` (marks origin) |
| sentiment_score | REAL | -1.0 to +1.0 |
| sentiment | TEXT | Single-word label (e.g., "frustrated") |
| entities_mentioned | TEXT[] | Named entities found in text |

Added: `sentiment_score`, `sentiment`, `entities_mentioned` — extracted at ingestion time by Flash Lite. Enables temporal sentiment queries ("how do I feel about Atna?") without polluting the graph with emotional nodes.

### `graph_nodes`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| label | TEXT | Canonical name |
| type | TEXT | `person`, `organization`, `project`, `place`, `animal` |
| normalized_label | TEXT | `LOWER(TRIM(label))` — unique, used for PostgREST upsert conflict target |
| metadata | JSONB | |
| created_at | TIMESTAMPTZ | |

Types: `person`, `organization`, `project`, `place`, `animal`. Types removed: `concept`, `emotional_state`, `resource`, `task`, `practice`, `cluster`.

### `graph_edges`

| Column | Type | Notes |
|--------|------|-------|
| id | INT (PK) | |
| source_id | INT | FK → `graph_nodes.id` |
| target_id | INT | FK → `graph_nodes.id` |
| relationship | TEXT | One of 16 valid types |
| weight | FLOAT | 0-1 confidence |
| source | TEXT | Extraction origin |
| source_memory_id | BIGINT | FK → `memories.id` |
| created_at | TIMESTAMPTZ | |
| status | TEXT | `active`, `expired`, `archived` |
| pending_id | INT | FK → `pending_graph_edges.id` linking back to the approval record |
| source_label | TEXT | Snapshot: original extracted label |
| target_label | TEXT | Snapshot: original extracted label |
| source_text | TEXT | `{table}:{id}` — provenance tracking |
| source_table | TEXT | `memories` or legacy `raw_dumps` |
| confidence | FLOAT | Extraction confidence |

Added: `status`, `pending_id`, `source_label`, `target_label`, `source_text`, `source_table`, `confidence` for provenance and lifecycle management.

## Supporting Tables

### `projects`

| Column | Type |
|--------|------|
| id | TEXT (PK) |
| name | TEXT |
| description | TEXT |
| status | TEXT |
| organization_name | TEXT |
| start_date | TIMESTAMPTZ |
| end_date | TIMESTAMPTZ |

### `people`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| name | TEXT | |
| organization_id | TEXT | |
| notes | TEXT | |
| role | TEXT | |
| strategic_weight | INTEGER | 1-10 |
| last_interaction_date | TIMESTAMPTZ | |
| last_pulse_reviewed | TIMESTAMPTZ | |
| source | TEXT | |
| graph_node_id | BIGINT | FK → `graph_nodes.id` (nullable) — links to knowledge graph |

Added: `graph_node_id` — bridges people registry ↔ knowledge graph. Added in Phase 1 to enable bidirectional lookup.

### `pending_graph_nodes`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| label | TEXT | |
| type | TEXT | `person`, `organization`, `project` |
| source | TEXT | |
| status | TEXT | `pending`, `approved`, `rejected` |
| created_at | TIMESTAMPTZ | |

Approved via Telegram `g{id}` shortcode with NLP corrections (e.g. "g2 is an organization, not a person"). RLS enabled.

### `pending_graph_edges`

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER (PK) | |
| source_label | TEXT | |
| target_label | TEXT | |
| relationship | TEXT | One of 16 valid types |
| source_text | TEXT | `{table}:{id}` |
| source_table | TEXT | `memories` (or legacy `raw_dumps`) |
| status | TEXT | `pending`, `approved`, `rejected` |
| confidence | REAL | |

RLS enabled. All backfill-suggested edges land here first. Approve/Edit/Reject via Decisions UI or Telegram `pe{id}` callbacks.

### `messages`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| content | TEXT | |
| source | TEXT | `whatsapp`, `email`, `call`, `telegram`, `teams` |
| classification | TEXT | Classification label |
| status | TEXT | `pending`, `approved`, `rejected` |
| created_at | TIMESTAMPTZ | |
| people | TEXT[] | People mentioned |

RLS enabled.

### `core_config`

KV store with `key:value` rows. Stores `last_pulse_summary` and other operational config.

### `dead_letter_queue`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| error | TEXT | |
| payload | JSONB | |
| created_at | TIMESTAMPTZ | |

RLS enabled.

### `system_audit_logs`

| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| action | TEXT | |
| entity_type | TEXT | |
| entity_id | TEXT | |
| details | JSONB | |
| created_at | TIMESTAMPTZ | |

RLS enabled.

### `user_settings`

| Column | Type | Notes |
|--------|------|-------|
| key | TEXT (PK) | |
| value | JSONB | |

## Resource Tables

### `resources`

| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | |
| title | TEXT | |
| url | TEXT | |
| type | TEXT | |
| category | TEXT | |
| tags | TEXT[] | |
| cluster_id | UUID | FK → `clusters.id` |
| created_at | TIMESTAMPTZ | |

### `clusters`

| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | |
| name | TEXT | |
| description | TEXT | |
| category | TEXT | |
| created_at | TIMESTAMPTZ | |

## Enum Domains

| Column | Values |
|--------|--------|
| `tasks.status` | pending, done, cancelled, superseded |
| `tasks.direction` | inbound, outbound, waiting_on |
| `tasks.priority` | critical, high, medium, low |
| `graph_nodes.type` | person, organization, project, place, animal |
| `graph_edges.relationship` | One of 16: DISCUSSED_WITH, MET_WITH, INTRODUCED, FRIEND_OF, PARENT_OF, SPOUSE_OF, SIBLING_OF, FAMILY_OF, PET_OF, MENTORS, WORKS_AT, WORKS_ON, CLIENT_OF, VENDOR_TO, MEMBER_OF, SERVES_AT |
| `memories.type` | Journal, note, outcome, reflection, relationship_note, archive |
| `people.source` | person, email, organization, initial_seed, unknown |
| `pending_graph_nodes.type` | person, organization, project |
| `messages.source` | whatsapp, email, call, telegram, teams |
| `messages.status` | pending, approved, rejected |

## Indexes

| Table | Index | Type |
|-------|-------|------|
| `memories` | `embedding` | Vector (IVFFlat) |
| `memories` | `created_at` | B-tree |
| `memories` | `type, created_at` | Composite B-tree |
| `memories` | `people` | GIN |
| `tasks` | `project_id` | B-tree |
| `tasks` | `created_at` | B-tree |
| `graph_nodes` | `normalized_label` | Unique B-tree |
| `graph_nodes` | `label` | B-tree (non-unique, after functional index migration) |
| `graph_edges` | `source_id, target_id` | Composite B-tree |
