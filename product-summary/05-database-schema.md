# 5. Database Schema (Supabase / PostgreSQL)

> **Re-baselined 2026-08-15** against the live schema (59 tables, pulled via the
> PostgREST OpenAPI spec from production). Previous versions of this doc listed
> 20 tables including dropped entities (`people`, `organizations`, `goals`,
> `pending_graph_nodes`, `entity_briefs`, `retrieval_triples`, …) — those are
> gone (see [Dropped tables](#dropped-tables)). If the live schema and this doc
> ever disagree again, the live schema wins — re-run the pull and fix the doc.

## Overview

- **Tables:** 59 in `public`.
- **Tenant scoping:** every data table carries `owner_id UUID` (the tenant).
  All app access flows through the tenant facade (`core/services/db.py` —
  `TenantAwareClient`, `tenant_table()`), which injects the caller's `owner_id`
  on writes and filters on reads. RLS grants were reworked in db/87–91 (anon
  revoked, per-tenant roles).
- **Auth:** service-role key server-side; RLS enforced for direct user access.
- **Embeddings:** `vector(768)` columns on `memories`, `graph_nodes`,
  `retrieval_passages`, `retrieval_phrase_nodes`, `canonical_pages`,
  `conversations`, `raw_dumps`, `resources`.
- **Migrations:** `db/` numbered SQL files (currently through **db/101**);
  replay-checked by the test suite's migration-replay gate.
- **Partitioning / background jobs:** `pending_*` tables are queues consumed by
  workers; no daily table partition currently exists (an earlier claim — `raw_dumps`
  daily partition trigger — is no longer true; `raw_dumps` is a plain table).

## Tenant & Auth

### `users`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | tenant identifier (`owner_id` everywhere) |
| email | TEXT | unique login identity |
| name | TEXT | display name (e.g. "Test" = canonical test tenant) |
| status | TEXT | `active`, … |
| telegram_chat_id | TEXT | bound chat for webhook routing |
| api_key_hash | TEXT | legacy key auth (replaced by OTP/Google sign-in) |
| google_connected | BOOLEAN | OAuth linked |
| monthly_credit_usd | NUMERIC | per-tenant LLM spend cap |
| credit_cycle_day | INTEGER | spend-cap billing day |
| created_at | TIMESTAMPTZ | |

### `user_settings`
| Column | Type | Notes |
|--------|------|-------|
| user_id | UUID (PK, FK → users.id) | |
| persona | TEXT | persona name |
| voice | TEXT | voice choice |
| timezone | TEXT | tz-aware anchoring |
| domains | JSONB | vocabulary domains |
| personal_orgs | JSONB | |
| onboarding_state | TEXT | sign-up progress |
| context | TEXT | |
| created_at / updated_at | TIMESTAMPTZ | |

### `user_oauth_tokens`
`id UUID PK`, `user_id UUID`, `provider TEXT` (google, …), `refresh_token TEXT`,
`scopes TEXT`, `created_at`/`updated_at`.

### `login_otps`
Email-OTP sign-in: `id UUID PK`, `email TEXT`, `code_hash TEXT`, `attempts INT`,
`expires_at`, `consumed_at`, `created_at`.

### `device_tokens`
Push tokens: `id`, `owner_id UUID`, `token TEXT`, `platform TEXT`,
`created_at`/`updated_at`.

### `org_creation_signals`
Candidate organization detections awaiting confirmation: `id BIGINT`,
`owner_id`, `org_name TEXT`, `source TEXT`, `task_id BIGINT`,
`raw_dump_id BIGINT`, `status`, `resolved_at`, `created_at`.

## Core Domain

### `tasks`
| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| owner_id | UUID | tenant |
| title | TEXT | task description |
| status | TEXT | `pending`, `done`, `cancelled`, `superseded` |
| priority | TEXT | `critical`, `high`, `medium`, `low` |
| direction | TEXT | `inbound`, `outbound`, `waiting_on` — who owns the action |
| deadline | DATE | |
| source | TEXT | `pulse`, `telegram`, `email`, `teams`, `whatsapp`, … |
| organization_id | UUID | FK → graph node (org) |
| project_id | BIGINT | FK → `projects.id` (dormant feature, see below) |
| google_event_id / google_task_id | TEXT | Google sync handles |
| is_revenue_critical | BOOLEAN | |
| committed_to / committed_on | TEXT / TIMESTAMPTZ | commitment owner + time |
| snooze_count / snoozed_until / snooze_feedback | INT / TIMESTAMPTZ / TEXT | snooze ladder |
| reminder_at | TIMESTAMPTZ | |
| recurrence | TEXT | |
| duration_mins / estimated_minutes | INT | |
| email_id | BIGINT | source email |
| dedup_key | VARCHAR | duplicate guard |
| is_current / supersedes_id / version | BOOLEAN / BIGINT / INT | version chain |
| created_at / updated_at / completed_at | TIMESTAMPTZ | |

### `raw_dumps`
| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | |
| owner_id | UUID | |
| content | TEXT | raw text (**not** `user_input` — renamed) |
| source | TEXT | `telegram`, `email`, `teams`, `whatsapp`, `web` (UI), `system`, `briefing` |
| message_type | TEXT | `normal`, `task`, `briefing`, … |
| direction | TEXT | inbound/outbound |
| status | TEXT | processing status (**not** `processed` boolean) |
| dedup_key | TEXT | duplicate guard |
| request_id | TEXT | prevents duplicate processing |
| sender | TEXT | |
| embedding | VECTOR(768) | |
| metadata | JSONB | |
| created_at | TIMESTAMPTZ | |

### `messages`
Unified inbound message envelope: `id BIGINT`, `owner_id`, `channel TEXT`
(whatsapp/email/call/telegram/teams/outlook), `source`, `sender_id`,
`sender_name`, `body TEXT`, `subject TEXT`, `summary TEXT`, `direction TEXT`,
`classification TEXT`, `processing_status TEXT`, `is_human_sender BOOLEAN`,
`has_memory_value BOOLEAN`, `needs_draft BOOLEAN`, `possible_duplicate BOOLEAN`,
`duplicate_of_title TEXT`, `suggested_title/suggested_project TEXT`,
`project_mapping_reason TEXT`, `project_confidence FLOAT8`, `linked_person_id UUID`,
`linked_project_id BIGINT`, `organization_id UUID`, `danny_decision TEXT`,
`rejection_reason TEXT`, `shown_in_brief BOOLEAN`, `embedding VECTOR(768)`,
`raw_payload JSONB`, `metadata JSONB`, `recording_id BIGINT`, `message_id TEXT`,
`thread_id TEXT`, `received_at`, `decided_at`, `expires_at`, `created_at`, `updated_at`.

### `conversation_threads`
Thread state per chat: `id UUID PK`, `owner_id`, `chat_id BIGINT`,
`thread_type` (enum `thread_type`), `entity_id/entity_label/entity_type TEXT`
(anchored entity), **`active_anchor JSONB`** (the live anchor — *not* on
`conversations.metadata`), `summary TEXT`, `routing_confidence TEXT`,
`last_decision_chain_id TEXT`, `last_active_at`, `archived_at`, `created_at`.

### `conversation_workflows`
Active multi-step conversations: `id UUID PK`, `owner_id`, `chat_id BIGINT`,
`thread_id UUID`, `workflow_type TEXT`, `status` (enum `workflow_status`),
`payload JSONB`, `awaiting_user_input BOOLEAN`, `expires_at`, `resolved_at`,
`created_at`/`updated_at`.

### `conversations`
Turn-level log: `id BIGINT`, `owner_id`, `chat_id BIGINT`, `thread_id UUID`,
`workflow_id UUID`, `role TEXT`, `content TEXT`, `intent TEXT`,
`entity_ids UUID[]`, `embedding VECTOR(768)`, `token_count INT`,
`session_id` (5-min session window), `metadata JSONB`, `created_at`.

### `awaiting_reply`
Snooze/escalation ladder for pending questions: `id BIGINT`, `owner_id`,
`chat_id TEXT`, `channel TEXT`, `question TEXT`, `status`, `asked_at`,
`expires_at`, `replied_at`, `linked_message_id BIGINT`, `created_at`/`updated_at`.

### `email_drafts`
Generated drafts: `id INT`, `owner_id`, `message_id BIGINT`, `draft_body TEXT`,
`status`, `reviewed_at`, `created_at`.

### `call_recordings`
Call capture: `id BIGINT`, `owner_id`, `drive_file_id/name`, `mime_type`,
`duration_seconds`, `transcript TEXT`, `extraction JSONB`, `status`,
`error_message`, `processed_at`, `metadata`, `created_at`.

### `canonical_pages`
Synthesis corpus pages: `id BIGINT`, `owner_id`, `title TEXT`, `content TEXT`,
`category TEXT`, `embedding VECTOR(768)`, `entity_id UUID`, `organization_id UUID`,
`project_id INT`, `is_current BOOLEAN`, `is_sparse BOOLEAN`, `version INT`,
`supersedes_id BIGINT`, `source_count INT`, `last_synth_at`, `is_archived`,
`archive_reason`, `archived_at`, `updated_at`.

### `resources` & `clusters`
- `resources`: `id BIGINT`, `owner_id`, `title`, `url`, `category`,
  `summary TEXT`, `strategic_note TEXT`, `embedding VECTOR(768)`,
  `organization_id UUID`, `project_id BIGINT`, `cluster_id BIGINT`,
  `is_current`, `supersedes_id INT`, `version`, `enriched_at`, `dismissed_at`,
  `created_at`.
- `clusters`: `id BIGINT`, `owner_id`, `title`, `description`, `status`,
  `created_at`.

## Knowledge Graph

### `graph_nodes`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | |
| owner_id | UUID | |
| label | TEXT | canonical name |
| normalized_label | TEXT | `LOWER(TRIM(label))`, unique per tenant |
| type | TEXT | `person`, `organization`, `project`, `place`, `animal`, … |
| canonical_id | UUID | canonical node in a merge chain |
| canonical_page_id | INT | FK → canonical_pages |
| embedding | VECTOR(768) | |
| epistemic_status | TEXT | known/uncertain |
| reference_count | INT | |
| db_record_id | TEXT | provenance |
| metadata | JSONB | enrichment, learn features |
| is_current / supersedes_id / version | BOOLEAN / UUID / INT | version chain |
| last_referenced_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |

### `graph_edges`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID (PK) | |
| owner_id | UUID | |
| source_node_id / target_node_id | UUID (FK → graph_nodes) | |
| relationship | TEXT | 16 valid types (see enum domain) |
| weight | FLOAT8 | 0–1 confidence |
| epistemic_status | TEXT | |
| source_ref | TEXT | provenance |
| metadata | JSONB | learn features (graph undo-training) |
| is_current / supersedes_id / version | BOOLEAN / UUID / INT | version chain |
| last_confirmed_at | TIMESTAMPTZ | |
| valid_from / valid_until | TIMESTAMPTZ | |
| archived | BOOLEAN | |
| created_at | TIMESTAMPTZ | |

### Graph approval & merge plumbing
- **`pending_nodes`** — suggested node approvals: `id BIGINT`, `owner_id`,
  `label TEXT`, `node_type TEXT`, `origin_id/origin_table`, `source_text`,
  `context`, `status`, `snooze_count/snoozed_until/snooze_feedback`,
  `clarification_id`, `eval_context JSONB`, `resolved_at`, `created_at`.
- **`pending_graph_edges`** — suggested edge approvals: `id INT`, `owner_id`,
  `source_label/source_node_id/source_type`, `target_label/target_node_id/target_type`,
  `relationship`, `confidence REAL`, `epistemic_status`, `source_ref/source_table/source_text`,
  `status`, `snooze_*`, `shortcode TEXT`, `approval_source`, `clarification_status`,
  `eval_context JSONB`, `evaluated_at`, `valid_from`, `created_at`.
- **`pending_graph_edges_archive`** — archived copy of the above (resolved rows).
- **`pending_graph_clarifications`** — clarification loop state: `id BIGINT`,
  `owner_id`, `chat_id BIGINT`, `label`, `pending_id INT`, `pending_type TEXT`,
  `step TEXT`, `context_json JSONB`, `status`, `claimed_at`, `expires_at`,
  `resolved_at`, `created_at`.
- **`merge_proposals`** — duplicate-node merge suggestions: `id BIGINT`,
  `owner_id`, `source_label/source_node_id/source_type`, `target_label/target_node_id`,
  `origin_id/origin_table`, `rationale`, `status`, `snooze_*`, `proposed_at`,
  `resolved_at`.
- **`graph_type_overrides`** — per-label type corrections: `owner_id`, `label`,
  `node_type`, `created_at`.

## Retrieval Subsystem (`core/retrieval/`)

- **`retrieval_passages`** — chunked passages: `id`, `owner_id`, `memory_id BIGINT`,
  `passage_index`, `text`, `raw_text`, `embedding VECTOR(768)`, `char_count`,
  `source_id/source_type/source_fingerprint`, `index_version`, `metadata`, `created_at`.
- **`retrieval_phrase_nodes`** — phrase-level index: `id`, `owner_id`,
  `normalized_text`, `display_text`, `node_type`, `embedding VECTOR(768)`,
  `search_vector TSVECTOR`, `first_seen_at`, `last_seen_at`, `metadata`.
- **`retrieval_edges`** — passage-graph edges: `id`, `owner_id`,
  `from_node_id/to_node_id BIGINT`, `edge_type`, `predicate_text`, `weight REAL`,
  `source_passage_id`, `index_version`, `created_at`.
- **`retrieval_alias_edges`** — alias links: `from_node_id/to_node_id`,
  `alias_type`, `weight`, `created_at`.
- **`retrieval_node_stats`** — `node_id`, `df`, `source_count`,
  `specificity_score REAL`, `updated_at`.
- **`retrieval_passage_phrase_links`** — `passage_id`, `node_id`, `role`, `weight`.
- **`retrieval_memory_bundle_links`** — `memory_id`, `passage_id`, `index_version`.
- **`retrieval_index_runs`** — indexing runs: `status`, `source_id/source_type/source_fingerprint`,
  `index_version`, `retry_count`, `error`, `started_at/completed_at`.
- **`retrieval_eval_runs` / `retrieval_eval_results` / `retrieval_eval_gold`** —
  retrieval evaluation harness (run metadata, per-query precision/recall/latency,
  golden query sets).

## Decisions & Learning Loop

### `decisions`
| Column | Type | Notes |
|--------|------|-------|
| id | INT (PK) | |
| owner_id | UUID | tenant |
| decision_type | TEXT | `approve`, `reject`, `snooze`, `confirm`, `undo`, … |
| status | TEXT | |
| title | TEXT | |
| source | TEXT | `telegram`, `api`, `app`, `pulse`, … |
| source_ref | TEXT | linked record (`table:id`) |
| entity_id / entity_type | TEXT | graph entity context |
| confidence | REAL | model confidence |
| rationale | TEXT | |
| context | TEXT | decision-time context |
| metadata | JSONB | **learn_features** — exact decision-time feature dict (graph undo-training, X2) |
| auto_decided | BOOLEAN | not user-initiated |
| reversible | BOOLEAN | |
| superseded_by | INT | undo chain |
| organization_id / project_id | INT | |
| expires_at | TIMESTAMPTZ | |
| decided_at / verified_at / created_at / updated_at | TIMESTAMPTZ | |

### `subsystem_patterns`
Per-subsystem learned patterns from the decision ledger: `id INT`, `owner_id`,
`subsystem TEXT`, `feature_hash TEXT`, `feature_json JSONB`, `confidence REAL`,
`total_count INT`, `correct_count INT`, `corrected_count INT`,
`soft_accepted_count INT`, `operator_endorsed_count INT`,
`first_seen`/`last_seen`.

### `subsystem_telemetry`
Prediction/outcome pairs: `id BIGINT`, `owner_id`, `subsystem TEXT`,
`event_type TEXT`, `features JSONB`, `predicted JSONB`, `actual JSONB`,
`outcome TEXT`, `confidence REAL`, `latency_ms INT`, `session_id TEXT`,
`source TEXT`, `created_at`.

### `clarification_feedback`
Clarification-loop answers: `id UUID`, `owner_id`, `question`, `answer`,
`question_type/response_type`, `source_id/source_table`, `shortcode TEXT`,
`sent_at`, `resolved_at`, `expires_at`, `created_at`.

### `classifier_corrections`
Intent-correction table: `text_pattern`, `old_intent`, `new_intent`, `count`,
`enabled BOOLEAN`, `created_by`, `first_seen/last_seen`, `owner_id`.

## Ops & Intelligence

### `core_config`
KV store: `id BIGINT`, `owner_id`, `key TEXT`, `content TEXT`, `updated_at`
(stores `last_pulse_summary` and other operational config).

### `app_intelligence`
Home-screen intelligence (20:00 Intel cron): `id BIGINT`, `owner_id`,
`home_mode TEXT`, `pulse_mode TEXT`, `pulse_run_id TEXT`, `context TEXT`,
`context_bar TEXT`, `top_focal_item JSONB`, `overdue_list/stale_list/nag_list JSONB`,
`vaulted_count INT`, `insights JSONB`, `delta_snapshot JSONB`, `metadata JSONB`,
`transparency_report TEXT`, `voice_line TEXT`, `created_at`.

### `pulse_runs`
Pulse run log: `id BIGINT`, `owner_id`, `pulse_type TEXT`, `trigger TEXT`,
`status`, `tasks_created INT`, `dumps_processed INT`, `metadata JSONB`,
`error_message`, `started_at`, `completed_at`, `failed_at`.

### `agent_queue`
Delegated-agent jobs: `id INT`, `owner_id`, `query`, `task`, `priority`,
`status`, `metadata JSONB`, `completed_at`, `created_at`.

### `processed_updates`
Idempotency guard for channel updates: `id UUID`, `owner_id`, `chat_id BIGINT`,
`update_id BIGINT`, `request_id TEXT`, `metadata JSONB`, `processed_at`.

### `pending_enrichment_jobs`
Enrichment queue: `id INT`, `owner_id`, `job_type`, `target_type/target_id`,
`content`, `related_id/related_org_id`, `status`, `retry_count`, `error`,
`started_at/completed_at`, `created_at`.

### `pending_retrieval_index_jobs`
Retrieval index queue: `id BIGINT`, `owner_id`, `memory_id/memory_type`,
`content`, `source`, `priority`, `status`, `retry_count`, `error`,
`started_at/completed_at`, `created_at`.

### `audit_logs`
Structured audit: `id BIGINT`, `owner_id`, `service`, `level`, `message`,
`metadata JSONB`, `created_at`.

### `system_audit_logs`
Operational audit: `id UUID`, `owner_id`, `event_type`, `function_name`,
`message`, `raw_input`, `created_at`.

### `dead_letter_queue`
Failed work: `id UUID`, `owner_id`, `content`, `failure_reason`,
`source_id/source_table`, `retry_count`, `resolved BOOLEAN`, `created_at`.

### `model_registry`
LLM call telemetry: `id UUID`, `owner_id`, `provider`, `model_name`,
`version`, `success BOOLEAN`, `latency_ms`, `input/output_tokens`,
`error_message`, `metadata`, `created_at`.

### `llm_spend`
Per-tenant LLM cost: `id BIGINT`, `owner_id`, `provider`, `model`,
`workload`, `outcome`, `input/output_tokens`, `est_cost_usd NUMERIC`, `ts`.

## Dormant (kept by decision — X1)

### `projects`
| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT (PK) | **not** TEXT |
| owner_id | UUID | |
| name | TEXT | |
| description / context | TEXT | |
| keywords | TEXT[] | |
| status | TEXT | |
| is_active / is_current | BOOLEAN | |
| organization_id | UUID | FK → graph node |
| parent_project_id | INT | |
| supersedes_id | BIGINT | version chain |
| version | INT | |
| created_at | TIMESTAMPTZ | |

**Status (2026-08-15): dormant.** 37 real rows, zero writers; retained because
the test harnesses use it as a live parent in FK-orphan sweeps and seeds, and
`graph_nodes.db_record_id` still references it. The old project→task assignment
and people↔project autocreation features (docs 10/11) are **decommissioned** —
do not treat this table as an active feature.

## Dropped tables

Removed by migrations; do not reference in new code or docs:

| Table | Dropped in | Superseded by |
|-------|-----------|---------------|
| `people` | db/75 | graph nodes + `graph_edges` |
| `organizations` | db/75 | graph nodes (org type) |
| `goals` | db/34 | task priority/direction + pulse |
| `person_aliases` | db/76 | `graph_nodes` alias handling |
| `pending_graph_nodes` | db/35 | `pending_nodes` |
| `entity_briefs` | db/101 | `graph_nodes.metadata` + on-demand context |
| `project_organizations` | db/101 | `tasks.organization_id` → graph |
| `retrieval_config` | db/101 | `core/retrieval/config.py` |
| `retrieval_triples` | db/101 | graph edges |
| `retrieval_passage_triple_links` | db/101 | graph edges |

Dropped RPCs (db/99, db/101): `match_canonical_pages`, `match_logs`,
`cleanup_expired_clarifications`, plus the retired clarifier-question flow.
Live RPC set is verified by `tests/tenants/test_db_isolation.py` (owner-scoped
RPC matrix) and the migration-replay gate.

## Enum Domains

| Column | Values |
|--------|--------|
| `tasks.status` | pending, done, cancelled, superseded |
| `tasks.direction` | inbound, outbound, waiting_on |
| `tasks.priority` | critical, high, medium, low |
| `conversation_threads.thread_type` | (enum `thread_type`) |
| `conversation_workflows.status` | (enum `workflow_status`) |
| `graph_edges.relationship` | 16 types: DISCUSSED_WITH, MET_WITH, INTRODUCED, FRIEND_OF, PARENT_OF, SPOUSE_OF, SIBLING_OF, FAMILY_OF, PET_OF, MENTORS, WORKS_AT, WORKS_ON, CLIENT_OF, VENDOR_TO, MEMBER_OF, SERVES_AT |
| `graph_nodes.type` | person, organization, project, place, animal, … |

## Indexes & Notes

- `graph_nodes.normalized_label` — unique per tenant (upsert conflict target).
- `memories.embedding`, `graph_nodes.embedding`, `retrieval_passages.embedding`,
  `retrieval_phrase_nodes.embedding`, `canonical_pages.embedding`,
  `conversations.embedding`, `raw_dumps.embedding`, `resources.embedding` —
  `vector(768)` with IVFFlat/HNSW index (see `core/retrieval/`).
- `conversation_threads(chat_id)`, `tasks(owner_id, status)`,
  `graph_edges(source_node_id, target_node_id)` — hot-path B-tree indexes.
- `memories` version-chains (`supersedes_id`, `is_current`, `version`) for
  memory pruning; `tasks`/`graph_nodes`/`resources`/`projects` follow the same
  pattern.

**Verification method:** tables/columns pulled from the production PostgREST
OpenAPI spec (`GET /rest/v1/` with `application/openapi+json`) on 2026-08-15.
