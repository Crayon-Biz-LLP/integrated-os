-- 78_tenant_scoping.sql
-- ============================================================================
-- M0: Multi-tenant scoping — plans/69-multi-tenant-product-plan.md
--
-- Creates the tenant model and scopes every data table to an owner:
--   1. users + user_settings  (the tenant model; users row = one tenant)
--   2. owner_id on all 58 data tables (verified against production dump,
--      2026-08-06; see backups/)
--   3. core_config uniqueness per (owner_id, key)  [was UNIQUE (key)]
--   4. owner_id indexes + FKs to users(id)
--
-- ⚠️ DEPLOYMENT ORDER (do not skip): this migration must land on production
--    TOGETHER WITH the M1–M3 code sweep, never ahead of it. Dropping
--    core_config_key_key breaks every live upsert that still says
--    on_conflict='key' — production would error on core_config writes until
--    the code moves to on_conflict='owner_id,key'. The branch + DB-copy
--    discipline in the plan exists for exactly this: validate here, deploy
--    schema + code as one coordinated release.
--
-- AFTER this migration, run scripts/migrate_danny_to_tenant1.py to backfill
-- owner_id for tenant #1 (Danny). That script then applies the SET NOT NULL
-- section at the bottom of this file (commented out here, executed there)
-- so the NOT NULL constraint only lands once every row is attributed.
-- ============================================================================


-- ── 1. Tenant model ────────────────────────────────────────────────────────

create table public.users (
    id            uuid primary key default gen_random_uuid(),
    name          text not null,
    email         text,
    api_key_hash  text unique,          -- hash of the per-user X-API-Key (M1)
    status        text not null default 'active',
    created_at    timestamptz not null default now()
);

create table public.user_settings (
    user_id          uuid primary key references public.users(id) on delete cascade,
    timezone         text,              -- IANA name, e.g. 'Asia/Kolkata'
    domains          jsonb,             -- routing taxonomy / life domains (M2)
    voice            text,              -- optional voice override (M2)
    context          text,              -- who they are, for prompt slots (M2)
    personal_orgs    jsonb,             -- personal/life org names for the work-life split (M2)
    onboarding_state text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);


-- ── 2. owner_id on all data tables ─────────────────────────────────────────
-- Required-scope tables (owner_id NOT NULL after backfill):

alter table public.agent_queue                    add column owner_id uuid;
alter table public.app_intelligence               add column owner_id uuid;
alter table public.call_recordings                add column owner_id uuid;
alter table public.canonical_pages                add column owner_id uuid;
alter table public.clarification_feedback         add column owner_id uuid;
alter table public.classifier_corrections         add column owner_id uuid;
alter table public.clusters                       add column owner_id uuid;
alter table public.conversation_threads           add column owner_id uuid;
alter table public.conversation_workflows         add column owner_id uuid;
alter table public.conversations                  add column owner_id uuid;
alter table public.core_config                    add column owner_id uuid;
alter table public.dead_letter_queue              add column owner_id uuid;
alter table public.decisions                      add column owner_id uuid;
alter table public.device_tokens                  add column owner_id uuid;
alter table public.email_drafts                   add column owner_id uuid;
alter table public.entity_briefs                  add column owner_id uuid;
alter table public.graph_edges                    add column owner_id uuid;
alter table public.graph_nodes                    add column owner_id uuid;
alter table public.graph_type_overrides           add column owner_id uuid;
alter table public.memories                       add column owner_id uuid;
alter table public.memory_cluster_members         add column owner_id uuid;
alter table public.memory_cluster_runs            add column owner_id uuid;
alter table public.memory_clusters                add column owner_id uuid;
alter table public.merge_proposals                add column owner_id uuid;
alter table public.messages                       add column owner_id uuid;
alter table public.org_creation_signals           add column owner_id uuid;
alter table public.pending_enrichment_jobs        add column owner_id uuid;
alter table public.pending_graph_clarifications   add column owner_id uuid;
alter table public.pending_graph_edges            add column owner_id uuid;
alter table public.pending_graph_edges_archive    add column owner_id uuid;
alter table public.pending_nodes                  add column owner_id uuid;
alter table public.pending_retrieval_index_jobs   add column owner_id uuid;
alter table public.processed_updates              add column owner_id uuid;
alter table public.project_organizations          add column owner_id uuid;
alter table public.projects                       add column owner_id uuid;
alter table public.pulse_runs                     add column owner_id uuid;
alter table public.raw_dumps                      add column owner_id uuid;
alter table public.resources                      add column owner_id uuid;
alter table public.retrieval_alias_edges          add column owner_id uuid;
alter table public.retrieval_config               add column owner_id uuid;
alter table public.retrieval_edges                add column owner_id uuid;
alter table public.retrieval_eval_gold            add column owner_id uuid;
alter table public.retrieval_eval_results         add column owner_id uuid;
alter table public.retrieval_eval_runs            add column owner_id uuid;
alter table public.retrieval_index_runs           add column owner_id uuid;
alter table public.retrieval_memory_bundle_links  add column owner_id uuid;
alter table public.retrieval_node_stats           add column owner_id uuid;
alter table public.retrieval_passage_phrase_links add column owner_id uuid;
alter table public.retrieval_passage_triple_links add column owner_id uuid;
alter table public.retrieval_passages            add column owner_id uuid;
alter table public.retrieval_phrase_nodes         add column owner_id uuid;
alter table public.retrieval_triples              add column owner_id uuid;
alter table public.subsystem_patterns             add column owner_id uuid;
alter table public.subsystem_telemetry            add column owner_id uuid;
alter table public.tasks                          add column owner_id uuid;

-- Attribution-only tables (owner_id stays NULLable — audit/meta):

alter table public.audit_logs          add column owner_id uuid;
alter table public.system_audit_logs   add column owner_id uuid;
alter table public.model_registry      add column owner_id uuid;


-- ── 3. core_config per-tenant uniqueness ────────────────────────────────────

-- Was: UNIQUE (key). Now: UNIQUE (owner_id, key) — the same key ('season',
-- 'app_version', ...) may exist per tenant. id stays the surrogate PK.
alter table public.core_config drop constraint core_config_key_key;
alter table public.core_config add constraint core_config_owner_key_key unique (owner_id, key);


-- ── 4. owner_id indexes ─────────────────────────────────────────────────────

create index if not exists idx_agent_queue_owner                    on public.agent_queue(owner_id);
create index if not exists idx_app_intelligence_owner               on public.app_intelligence(owner_id);
create index if not exists idx_audit_logs_owner                     on public.audit_logs(owner_id);
create index if not exists idx_call_recordings_owner                on public.call_recordings(owner_id);
create index if not exists idx_canonical_pages_owner                on public.canonical_pages(owner_id);
create index if not exists idx_clarification_feedback_owner         on public.clarification_feedback(owner_id);
create index if not exists idx_classifier_corrections_owner         on public.classifier_corrections(owner_id);
create index if not exists idx_clusters_owner                       on public.clusters(owner_id);
create index if not exists idx_conversation_threads_owner           on public.conversation_threads(owner_id);
create index if not exists idx_conversation_workflows_owner         on public.conversation_workflows(owner_id);
create index if not exists idx_conversations_owner                  on public.conversations(owner_id);
create index if not exists idx_core_config_owner                    on public.core_config(owner_id);
create index if not exists idx_dead_letter_queue_owner              on public.dead_letter_queue(owner_id);
create index if not exists idx_decisions_owner                      on public.decisions(owner_id);
create index if not exists idx_device_tokens_owner                  on public.device_tokens(owner_id);
create index if not exists idx_email_drafts_owner                   on public.email_drafts(owner_id);
create index if not exists idx_entity_briefs_owner                  on public.entity_briefs(owner_id);
create index if not exists idx_graph_edges_owner                    on public.graph_edges(owner_id);
create index if not exists idx_graph_nodes_owner                    on public.graph_nodes(owner_id);
create index if not exists idx_graph_type_overrides_owner           on public.graph_type_overrides(owner_id);
create index if not exists idx_memories_owner                       on public.memories(owner_id);
create index if not exists idx_memory_cluster_members_owner         on public.memory_cluster_members(owner_id);
create index if not exists idx_memory_cluster_runs_owner            on public.memory_cluster_runs(owner_id);
create index if not exists idx_memory_clusters_owner                on public.memory_clusters(owner_id);
create index if not exists idx_merge_proposals_owner                on public.merge_proposals(owner_id);
create index if not exists idx_messages_owner                       on public.messages(owner_id);
create index if not exists idx_model_registry_owner                 on public.model_registry(owner_id);
create index if not exists idx_org_creation_signals_owner           on public.org_creation_signals(owner_id);
create index if not exists idx_pending_enrichment_jobs_owner        on public.pending_enrichment_jobs(owner_id);
create index if not exists idx_pending_graph_clarifications_owner   on public.pending_graph_clarifications(owner_id);
create index if not exists idx_pending_graph_edges_owner            on public.pending_graph_edges(owner_id);
create index if not exists idx_pending_graph_edges_archive_owner    on public.pending_graph_edges_archive(owner_id);
create index if not exists idx_pending_nodes_owner                  on public.pending_nodes(owner_id);
create index if not exists idx_pending_retrieval_index_jobs_owner   on public.pending_retrieval_index_jobs(owner_id);
create index if not exists idx_processed_updates_owner              on public.processed_updates(owner_id);
create index if not exists idx_project_organizations_owner          on public.project_organizations(owner_id);
create index if not exists idx_projects_owner                       on public.projects(owner_id);
create index if not exists idx_pulse_runs_owner                     on public.pulse_runs(owner_id);
create index if not exists idx_raw_dumps_owner                      on public.raw_dumps(owner_id);
create index if not exists idx_resources_owner                      on public.resources(owner_id);
create index if not exists idx_retrieval_alias_edges_owner          on public.retrieval_alias_edges(owner_id);
create index if not exists idx_retrieval_config_owner               on public.retrieval_config(owner_id);
create index if not exists idx_retrieval_edges_owner                on public.retrieval_edges(owner_id);
create index if not exists idx_retrieval_eval_gold_owner            on public.retrieval_eval_gold(owner_id);
create index if not exists idx_retrieval_eval_results_owner         on public.retrieval_eval_results(owner_id);
create index if not exists idx_retrieval_eval_runs_owner            on public.retrieval_eval_runs(owner_id);
create index if not exists idx_retrieval_index_runs_owner           on public.retrieval_index_runs(owner_id);
create index if not exists idx_retrieval_memory_bundle_links_owner  on public.retrieval_memory_bundle_links(owner_id);
create index if not exists idx_retrieval_node_stats_owner           on public.retrieval_node_stats(owner_id);
create index if not exists idx_retrieval_passage_phrase_links_owner on public.retrieval_passage_phrase_links(owner_id);
create index if not exists idx_retrieval_passage_triple_links_owner on public.retrieval_passage_triple_links(owner_id);
create index if not exists idx_retrieval_passages_owner             on public.retrieval_passages(owner_id);
create index if not exists idx_retrieval_phrase_nodes_owner         on public.retrieval_phrase_nodes(owner_id);
create index if not exists idx_retrieval_triples_owner              on public.retrieval_triples(owner_id);
create index if not exists idx_subsystem_patterns_owner             on public.subsystem_patterns(owner_id);
create index if not exists idx_subsystem_telemetry_owner            on public.subsystem_telemetry(owner_id);
create index if not exists idx_system_audit_logs_owner              on public.system_audit_logs(owner_id);
create index if not exists idx_tasks_owner                          on public.tasks(owner_id);


-- ── 5. Foreign keys to users(id) ───────────────────────────────────────────

alter table public.agent_queue                    add constraint fk_agent_queue_owner                    foreign key (owner_id) references public.users(id);
alter table public.app_intelligence               add constraint fk_app_intelligence_owner               foreign key (owner_id) references public.users(id);
alter table public.audit_logs                     add constraint fk_audit_logs_owner                     foreign key (owner_id) references public.users(id);
alter table public.call_recordings                add constraint fk_call_recordings_owner                foreign key (owner_id) references public.users(id);
alter table public.canonical_pages                add constraint fk_canonical_pages_owner                foreign key (owner_id) references public.users(id);
alter table public.clarification_feedback         add constraint fk_clarification_feedback_owner         foreign key (owner_id) references public.users(id);
alter table public.classifier_corrections         add constraint fk_classifier_corrections_owner         foreign key (owner_id) references public.users(id);
alter table public.clusters                       add constraint fk_clusters_owner                       foreign key (owner_id) references public.users(id);
alter table public.conversation_threads           add constraint fk_conversation_threads_owner           foreign key (owner_id) references public.users(id);
alter table public.conversation_workflows         add constraint fk_conversation_workflows_owner         foreign key (owner_id) references public.users(id);
alter table public.conversations                  add constraint fk_conversations_owner                  foreign key (owner_id) references public.users(id);
alter table public.core_config                    add constraint fk_core_config_owner                    foreign key (owner_id) references public.users(id);
alter table public.dead_letter_queue              add constraint fk_dead_letter_queue_owner              foreign key (owner_id) references public.users(id);
alter table public.decisions                      add constraint fk_decisions_owner                      foreign key (owner_id) references public.users(id);
alter table public.device_tokens                  add constraint fk_device_tokens_owner                  foreign key (owner_id) references public.users(id);
alter table public.email_drafts                   add constraint fk_email_drafts_owner                   foreign key (owner_id) references public.users(id);
alter table public.entity_briefs                  add constraint fk_entity_briefs_owner                  foreign key (owner_id) references public.users(id);
alter table public.graph_edges                    add constraint fk_graph_edges_owner                    foreign key (owner_id) references public.users(id);
alter table public.graph_nodes                    add constraint fk_graph_nodes_owner                    foreign key (owner_id) references public.users(id);
alter table public.graph_type_overrides           add constraint fk_graph_type_overrides_owner           foreign key (owner_id) references public.users(id);
alter table public.memories                       add constraint fk_memories_owner                       foreign key (owner_id) references public.users(id);
alter table public.memory_cluster_members         add constraint fk_memory_cluster_members_owner         foreign key (owner_id) references public.users(id);
alter table public.memory_cluster_runs            add constraint fk_memory_cluster_runs_owner            foreign key (owner_id) references public.users(id);
alter table public.memory_clusters                add constraint fk_memory_clusters_owner                foreign key (owner_id) references public.users(id);
alter table public.merge_proposals                add constraint fk_merge_proposals_owner                foreign key (owner_id) references public.users(id);
alter table public.messages                       add constraint fk_messages_owner                       foreign key (owner_id) references public.users(id);
alter table public.model_registry                 add constraint fk_model_registry_owner                 foreign key (owner_id) references public.users(id);
alter table public.org_creation_signals           add constraint fk_org_creation_signals_owner           foreign key (owner_id) references public.users(id);
alter table public.pending_enrichment_jobs        add constraint fk_pending_enrichment_jobs_owner        foreign key (owner_id) references public.users(id);
alter table public.pending_graph_clarifications   add constraint fk_pending_graph_clarifications_owner   foreign key (owner_id) references public.users(id);
alter table public.pending_graph_edges            add constraint fk_pending_graph_edges_owner            foreign key (owner_id) references public.users(id);
alter table public.pending_graph_edges_archive    add constraint fk_pending_graph_edges_archive_owner    foreign key (owner_id) references public.users(id);
alter table public.pending_nodes                  add constraint fk_pending_nodes_owner                  foreign key (owner_id) references public.users(id);
alter table public.pending_retrieval_index_jobs   add constraint fk_pending_retrieval_index_jobs_owner   foreign key (owner_id) references public.users(id);
alter table public.processed_updates              add constraint fk_processed_updates_owner              foreign key (owner_id) references public.users(id);
alter table public.project_organizations          add constraint fk_project_organizations_owner          foreign key (owner_id) references public.users(id);
alter table public.projects                       add constraint fk_projects_owner                       foreign key (owner_id) references public.users(id);
alter table public.pulse_runs                     add constraint fk_pulse_runs_owner                     foreign key (owner_id) references public.users(id);
alter table public.raw_dumps                      add constraint fk_raw_dumps_owner                      foreign key (owner_id) references public.users(id);
alter table public.resources                      add constraint fk_resources_owner                      foreign key (owner_id) references public.users(id);
alter table public.retrieval_alias_edges          add constraint fk_retrieval_alias_edges_owner          foreign key (owner_id) references public.users(id);
alter table public.retrieval_config               add constraint fk_retrieval_config_owner               foreign key (owner_id) references public.users(id);
alter table public.retrieval_edges                add constraint fk_retrieval_edges_owner                foreign key (owner_id) references public.users(id);
alter table public.retrieval_eval_gold            add constraint fk_retrieval_eval_gold_owner            foreign key (owner_id) references public.users(id);
alter table public.retrieval_eval_results         add constraint fk_retrieval_eval_results_owner         foreign key (owner_id) references public.users(id);
alter table public.retrieval_eval_runs            add constraint fk_retrieval_eval_runs_owner            foreign key (owner_id) references public.users(id);
alter table public.retrieval_index_runs           add constraint fk_retrieval_index_runs_owner           foreign key (owner_id) references public.users(id);
alter table public.retrieval_memory_bundle_links  add constraint fk_retrieval_memory_bundle_links_owner  foreign key (owner_id) references public.users(id);
alter table public.retrieval_node_stats           add constraint fk_retrieval_node_stats_owner           foreign key (owner_id) references public.users(id);
alter table public.retrieval_passage_phrase_links add constraint fk_retrieval_passage_phrase_links_owner foreign key (owner_id) references public.users(id);
alter table public.retrieval_passage_triple_links add constraint fk_retrieval_passage_triple_links_owner foreign key (owner_id) references public.users(id);
alter table public.retrieval_passages            add constraint fk_retrieval_passages_owner             foreign key (owner_id) references public.users(id);
alter table public.retrieval_phrase_nodes         add constraint fk_retrieval_phrase_nodes_owner         foreign key (owner_id) references public.users(id);
alter table public.retrieval_triples              add constraint fk_retrieval_triples_owner              foreign key (owner_id) references public.users(id);
alter table public.subsystem_patterns             add constraint fk_subsystem_patterns_owner             foreign key (owner_id) references public.users(id);
alter table public.subsystem_telemetry            add constraint fk_subsystem_telemetry_owner            foreign key (owner_id) references public.users(id);
alter table public.system_audit_logs              add constraint fk_system_audit_logs_owner              foreign key (owner_id) references public.users(id);
alter table public.tasks                          add constraint fk_tasks_owner                          foreign key (owner_id) references public.users(id);


-- ═══ FINALIZE (executed by scripts/migrate_danny_to_tenant1.py --apply) ═══
-- Once every row is attributed to tenant #1, the required-scope tables get
-- owner_id NOT NULL:
--
--   alter table public.agent_queue                    alter column owner_id set not null;
--   ... (55 tables) ...
--   alter table public.tasks                          alter column owner_id set not null;
--
-- audit_logs / system_audit_logs / model_registry intentionally stay NULLable
-- (attribution-only). Do NOT run this by hand — the migration script guards
-- it and verifies 0 NULL owners first.
