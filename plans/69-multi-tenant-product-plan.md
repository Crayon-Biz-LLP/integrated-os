# 69 — Multi-Tenant Product Plan

**Status:** Approved — architecture locked
**Decision:** Convert THIS repo in-place to a shared multi-tenant system. Same repo, same Supabase
project, same Modal backend, same cron jobs. Danny is tenant #1.
**Goal:** 10 users this quarter, 50+ without re-architecting.

---

## 1. Why (the product decision)

- Operating N isolated instances (N Supabase projects, N Modal apps, N secrets, N cron accounts) is
  operationally maddening at 10 and impossible at 50. One shared system is the only path to scale.
- Danny = tenant #1: deepest dataset, most hardened code, real daily usage, and a permanent
  regression test for the product (his daily use exercises every path).
- The de-personalization pass does NOT strip Danny's personalization — it relocates it into a
  per-tenant `user_settings` row (taxonomy, domains, timezone, voice, context). His experience is
  preserved; the product inherits a reference configuration that proves the personalization system.
- Android app stays the primary channel. Telegram remains an optional graceful channel.

## 2. Target architecture (one of everything)

| Asset | Current | Target |
|---|---|---|
| Repo | this repo | this repo (in-place conversion) |
| Supabase | 1 project (Danny's data) | same project; `users` + `owner_id` on every table |
| Modal | 1 app (`rhodey-os`) | same app; tenant resolved per request |
| Cron | cron-job.org (sentinel 5m, decision-pulse 30m, roundup 2x/d) | same 3 jobs; each run iterates tenants |
| Android app | 1 APK | same APK; Settings holds per-user API key |
| FCM | 1 Firebase project | same; `device_tokens.owner_id` scopes pushes |
| Google | 1 refresh token (env) | per-tenant tokens in DB |
| LLM keys | env (shared) | shared now; per-tenant model routing LATER |

## 3. Current-state audit (verified)

- **169+** `supabase.table(...)` call sites across `core/`, `api/`, plus scripts/tests — none scoped.
- **22+** `.rpc(...)` calls to ~20 Postgres functions (`match_memories`, `match_graph_nodes`,
  `search_phrase_nodes`, `batch_whatsapp_message`, `claim_pending_enrichment_job`, `detect_drift`,
  `expire_stale_graph_edges`, `get_most_connected_nodes`, `next_clarification_shortcode`,
  `archive_terminal_pending_edges`, `match_resources`, `match_emails_hybrid`, `match_whatsapp_hybrid`,
  `match_conversations`, `find_serendipity_paths`, `cleanup_expired_clarifications`, `run_sql` …).
- **0** existing tenant scoping. The only "user" is the `TELEGRAM_CHAT_ID` env var.
- Data access flows through one chokepoint: `core/services/db.py` (`get_supabase()` singleton,
  `exec_query`, `maybe_single_safe`). `core/services/async_db.py` (asyncpg pool) covers hot paths.
- `core_config` is global-keyed (`season`, `briefing_history`, `app_version`, dismissed practices).
- Knowledge graph (`graph_nodes`, `graph_edges`) is global; retrieval (vector + PPR + context
  assembly) is global. **This is the highest-risk area** — n-gram/entity resolution can collide
  across tenants (see `test_ngrams.py`).
- Learning loop (`classifier_corrections`, `decisions`, `subsystem_telemetry`) is global.

## 4. Tenancy model

- **`users` table** — id, name, api_key (hashed), status, created_at. Seeded first row = Danny.
- **`user_settings` JSONB column or table** — name, timezone, domains/taxonomy, voice, context,
  onboarding state.
- **Auth:** `require_api_auth(request)` resolves `X-API-Key` → `user_id` (existing header plumbing).
  Later: Supabase Auth login in the app replaces the raw key.
- **Scoping strategy (code-first, RLS second):**
  1. `contextvars` tenant context set once per request at the API layer.
  2. Scoped builder in `core/services/db.py`: `table(name)` → auto-injects `.eq('owner_id', uid)`;
     `rpc(name, args)` → injects `owner_id`; writes set `owner_id` implicitly.
  3. Sweep all 169 call sites module-by-module onto the scoped builder.
  4. **RLS policies are the SECOND layer (hardening pass, later)** — per AGENTS.md standards:
     code scoping is *heavily reduced risk*; RLS is the *structurally enforced* guarantee.
- **`core_config`** PK becomes `(owner_id, key)`.
- **Shared/global tables** (no owner): `users`, `model_registry` (audit), `audit_logs` (audit, with
  `owner_id` for attribution).

## 5. Migration safety (non-negotiable)

1. All work on a branch; `main` stays deployed and serving Danny until green.
2. Migrate from a **COPY of the production DB**, never live.
3. Gates before cutover: full test suite green, UAT suite green on the copy, **Danny's own briefing
   reads identically post-migration** (voice, taxonomy, domains).
4. Cutover = one deploy. Rollback = revert the branch (git history + RLS are the safety nets).

## 6. Build sequence

### M0 — Schema & tenant bootstrap (3–5 days)
- `db/78_tenant_scoping.sql`: `users`, `user_settings`, `owner_id` column on ~80 tables, `core_config`
  PK → `(owner_id, key)`, indexes on `owner_id`, `device_tokens.owner_id`.
- `scripts/bootstrap_tenant.py`: create tenant + seed `user_settings` + root graph node.
- `scripts/migrate_danny_to_tenant1.py`: backfill `owner_id = danny_user_id` on all rows (dry-run first,
  [UAT]-style verification, cleanup).
- Exit: copy-DB migration runs clean; Danny's data all attributed; existing tests still green on copy.

### M1 — Scoped data layer (3–5 days) — the enabler
- `core/services/db.py`: `TenantContext` (contextvar), `tenant_table()` builder, `tenant_rpc()`
  wrapper, `require_user()` helper; `get_supabase()` untouched for scripts/tests.
- `api/index.py`: `require_api_auth` resolves key → user and sets tenant context (middleware).
- Tenant #1 (Danny) can go live on the branch **the moment M1 lands** — before M2/M3 polish.
- Exit: an unauthenticated/unknown key is rejected; every table read auto-scopes; test:
  tenant isolation smoke test.

### M2 — De-personalization → user_settings (3–5 days)
- Template the ~20 prompt files that say "Danny" → `{name}`/`{context}` slots fed from `user_settings`
  (`USER_NAME` env already exists as the fallback).
- Routing taxonomy (`core/prompts/classify.py`), briefing sections + `personal_orgs`
  (`core/pulse/briefing.py:517`), decision-pulse openers → per-tenant domains config.
- Timezone (`core/lib/time_utils.py`, `core/services/google_service.py`, pulse schedules) →
  `user_settings.timezone` (fallback `Asia/Kolkata`).
- Voice (`core/prompts/voice.py`, `core/lib/rhodey_voice.py`) → per-tenant override optional.
- Exit: fresh tenant without settings still works (defaults); Danny's settings reproduce his
  current briefing byte-for-byte on the copy DB.

### M3 — The sweep: scope every query path (1–1.5 weeks) — the big one

**M3 progress (sweep COMPLETE — 2026-08-06):** all query paths now flow
through the tenant layer. Strategy: module bindings swap at the chokepoint,
not a 169-site rewrite — each swept module binds `supabase =
tenant_aware_client()` (or a per-function facade for modules with function-
level imports) instead of `get_supabase()`.

**Layer 1 — facade (`core/services/db.py`):**
- `TenantAwareClient` facade routes `.table()`/`.rpc()` through
  `tenant_table()`/`tenant_rpc()` when tenant mode is on, fail-closed via
  `require_tenant()`; legacy unscoped pre-db/78.
- `tenant_mode_enabled()` probe; `resolve_channel_tenant()` (single active
  user → tenant for Telegram/cron traffic).
- `channel_tenant_scope()`: generic cron/Telegram scope (no-op when a tenant
  context is already active, e.g. nested under an API-key scope).
- `TenantTable.select()/update()/delete()` apply the owner filter on the
  REAL chain (the M1 construction-time `.eq()` was rejected by supabase-py).
- `GLOBAL_RPCS` registry: admin/global functions (`next_clarification_shortcode`,
  `run_sql`) never get `owner_id` injected.

**Layer 2 — entry points wrapped (each establishes its own scope):**
- Webhook (M3 part 1): `process_webhook`, `process_channel_pending_decision`,
  `process_email_pending_decision`, `send_draft_reply`, `handle_ed_command`,
  `route_by_intent`, `process_multimodal_content`.
- Pulse (this pass): `process_pulse` (briefing.py), `process_decision_pulse`,
  `process_sentinel`, `run_full_health_check` (pipeline.py). Wrapper pattern:
  public scope-setting wrapper + renamed inner impl.
- `api/index.py`: `require_api_auth` now RETURNS the resolved uid and keeps
  the tenant context set for the request (M1 restore-on-exit contract
  deliberately replaced); local bindings swapped to the facade; cron
  roundup endpoint scoped via `channel_tenant_scope()`.

**Layer 3 — module bindings swept (~50 files):**
- `core/pulse/*` (16 files): llm, context, memory, memory_clusters, graph,
  practices, tools, patterns, resources, entity_extractor, calendar,
  maintenance, briefing, pipeline, decision_pulse, sentinel.
- `core/skills/*` (10): email/archive/teams/outlook/whatsapp/call_ingest,
  brain_synth_v2, dedupe_pending, backfill_graph, dlq_consumer.
- `core/retrieval/*` (6): search, graph, pipeline, backfill, eval,
  seed_eval_gold.
- `core/lib/*` (12): node_tables, graph_rules, ingest, temporal_lineage,
  enrichment_queue, url_filter, clarification_state, entity_linker,
  entity_detector, telemetry, people_utils, conversation, decision_features,
  planner_critic, pattern_extractor.
- `core/actions/*` (executor — per-function facades, planner),
  `core/agents/*` (research_agent, cleanup_orphans), `core/decisions.py`,
  `core/clarifier.py`, `core/pulse_cli.py`.
- `core/lib/audit_logger.py` intentionally keeps unscoped `get_supabase()`
  (global audit infra; owner_id recorded for attribution).

**Layer 4 — RPC scoping (db/80 + db/81):**
- 15 data RPCs gain `owner_id uuid DEFAULT NULL` + table-qualified
  `WHERE … owner_id = p_owner` filter (legacy calls keep working).
- **Two real leaks found & fixed while sweeping:** (1) `LANGUAGE sql`
  functions (`match_resources`, `match_conversations`) resolve the param
  to the COLUMN — `owner_id = owner_id` is always true → cross-tenant leak;
  converted both to plpgsql with a `p_owner` snapshot (PL/pgSQL params are
  ambiguous against columns on PG17; unqualified filters hard-error, so the
  DECLARE-snapshot pattern is mandatory for ALL scoped RPCs).
  (2) `archive_terminal_pending_edges` INSERT SELECT broke on column drift
  (`snoozed_until` from db/72, `owner_id` from db/78) → explicit column
  lists. INSERT-heavy RPCs (`archive_terminal_pending_edges`,
  `batch_whatsapp_message`) rename the param to `p_owner`; facade maps
  per-RPC owner param name.

**db/82 (post-review catch):** code review cross-checked every `.rpc()` call
site against the scoped list and found TWO graph RPCs the sweep missed —
`match_graph_nodes` (graph.py:1001) and `get_most_connected_nodes`
(graph.py:1061). The latter was `LANGUAGE sql` with NO owner filter at all
— the exact silent-leak class (facade would have failed them loudly at
runtime; the sql one would have leaked). Both now scoped with the p_owner
pattern; isolation proven on copy DB and added to the remaining-isolation
gate (5 RPC isolation checks total).

**Verification — both gates green + 47 unit tests:**
- `scripts/verify_m3_webhook_isolation.py`: 10 gates (read/write/rpc
  scoping, fail-closed, schema-level isolation on copy DB).
- `scripts/verify_m3_remaining_isolation.py`: channel scope, 14 module
  facades are TenantAwareClient, facade owner injection + global-RPC
  carve-out, fail-closed (table + rpc), RPC isolation on copy DB
  (`search_phrase_nodes`, `claim_pending_enrichment_job`).
- `python3 -m py_compile` + `ruff check` clean on all changed files;
  `tests/unit/test_tenant_scope.py` + `test_user_settings.py` +
  `test_email_classify_prompt.py` = 47 passed.
- RPC smoke on copy DB: `match_resources`/`match_conversations` isolation
  proven with two-owner rows; `archive_terminal_pending_edges` and
  `batch_whatsapp_message` insert with correct owner attribution.

Still to sweep: remaining match_*/search/claim RPCs
(deliberately deferred to M4/M5). `api/briefing.py` is already covered —
its `build_briefing(supabase)` is called from api/index.py with the
tenant-aware facade at every call site (313/490/587/3229) and has no internal
get_supabase(); a defensive `supabase=None → tenant_aware_client()` fallback
makes it fail closed even for future callers.
- Module-by-module grep-driven sweep onto the scoped builder (verify with existing tests as you go):
  `api/index.py`, `api/briefing.py`, `core/webhook/*` (handler, dispatch, classify, email, utils,
  workflows, graph, feedback_loop), `core/pulse/*` (briefing, sentinel, decision_pulse, context,
  memory, graph, tools, calendar, resources, practices, maintenance, pipeline, entity_extractor,
  memory_clusters), `core/skills/*` (email_ingest, call_ingest, whatsapp_ingest, outlook_ingest,
  teams_ingest, brain_synth_v2, dlq_consumer, backfill_graph), `core/retrieval/*` (search, graph,
  pipeline, backfill), `core/actions/*`, `core/lib/*` (enrichment_queue, temporal_lineage,
  clarification_state), `core/clarifier.py`, `core/decisions.py`, `core/agents/*`.
- RPCs: add `owner_id` param + `WHERE owner_id = …` to all match_* / search / claim / state
  functions; `run_sql` stays admin-only.
- Graph & retrieval: `owner_id` on nodes/edges + every PPR/vector/context filter; entity resolution
  and n-gram matching become tenant-scoped (add cross-tenant contamination tests).
- Exit: suite green; isolation tests pass; Danny's flows verified on copy.

### M4 — Cron fan-out + per-tenant push (2–3 days)

**M4 progress (COMPLETE — 2026-08-06):** one cron run now serves all
tenants; pushes land only on the right user's devices.

- **Fan-out primitives (`core/services/db.py`):** `active_user_ids()` (all
  users.status='active', oldest first; [] → legacy unscoped), and
  `core_config_upsert()` — picks `on_conflict` by tenant mode
  (`owner_id,key` vs legacy `key`), because db/78 changed the core_config
  PK and every bare `on_conflict='key'` would 400 post-migration. Also
  `resolve_telegram_chat_id()`: per-user `users.telegram_chat_id` → env
  fallback while exactly one active user exists → None in multi-user
  (app-only tenants never inherit someone else's chat).
- **`db/83_users_telegram_chat.sql`:** `users.telegram_chat_id text` —
  per-tenant optional Telegram channel. Applied to copy DB.
- **Entry points fanned out** (wrapper iterates `active_user_ids()`, runs
  the impl once per user under `tenant_scope(uid)`, isolates per-tenant
  failures): `process_sentinel`, `process_decision_pulse`, and the
  `/api/roundup` route (per-tenant notes check + per-tenant chat id).
  Legacy (no users table / no active user) runs once unscoped — identical
  to pre-M4 behaviour.
- **Per-tenant push (`core/services/push_notification.py`):** device_tokens
  reads + invalid-token deletes now owner-scoped via `get_tenant()`;
  extracted `scoped_tokens_query()` shared by both send paths. Unscoped
  legacy paths keep querying all tokens.
- **Per-tenant rate limiting:** `last_decision_push_fp` fingerprint upsert
  now goes through `core_config_upsert()` — the (owner_id, key) PK makes
  the fingerprint per-user automatically. Also fixed the other 4 latent
  `on_conflict='key'` 400s (pipeline heartbeat, practices dismissed list,
  telemetry baseline, sentinel weekly_patterns).
- **Per-tenant personalization in pulse:** decision-pulse opener now greets
  `resolve_user_name(current_user_id())` — previously the env/default name
  (would have said "Danny" to every tenant). Sentinel/decision-pulse/
  roundup resolve the chat id per tenant and `send_telegram()` skips
  gracefully when it's absent (app-only users).
- **Audit attribution (`core/lib/audit_logger.py`):** audit writes now
  stamp `owner_id` from the tenant context. This matters because sentinel's
  dedup gates (weekly sweep, pattern detection, DLQ, enrichment, etc.) read
  `audit_logs` through the facade (owner-scoped) — without the stamp they
  would never match and every sweep would run every cycle.

**Verification — `scripts/verify_m4_cron_fanout.py` (12 gates):**
- copy DB has active users; `active_user_ids()` parses correctly
- sentinel + decision-pulse fan out once per user with the right tenant
  set; legacy path runs once unscoped
- roundup route uses `active_user_ids()` + `tenant_scope(uid)`
- `core_config_upsert` conflict target flips by tenant mode
- telegram resolution: per-user value wins, single-user env fallback,
  multi-user without value → None (no cross-tenant leak)
- push token query owner filter present; audit owner stamp resolves
- Plus: `python3 -m py_compile` + `ruff check` clean; 47 unit tests green;
  both M3 isolation gates still pass.

Remaining for M4 (deliberate): `process_pulse` (briefing) and
`run_full_health_check` still run under the single channel tenant — they
are the heavy LLM paths and fan out in the same wrapper pattern when M6
per-tenant cost controls land.

### M5 — Onboarding (1 week) — the cold-start killer

**M5-A + M5-B progress (COMPLETE — 2026-08-06):** a new tenant now goes
from invite → API key → Google connect → seeded world → first briefing
without any single-user code paths.

- **Per-tenant Google OAuth (`db/84_user_oauth_tokens.sql`):**
  `user_oauth_tokens (user_id, provider, refresh_token, scopes, updated_at)`
  + `users.google_connected`. Applied to copy DB.
- **Tenant-aware credential layer (`core/services/google_service.py`):**
  `get_google_creds(user_id)` / `get_cached_service(...)` resolve the token
  per tenant (tenant context from M4 fan-out), cached per user id (never
  keyed on the implicit context inside an lru_cache — that would collapse
  every tenant onto one slot), env fallback for legacy single-user mode,
  and **None when a tenant has no token** — all callers skip gracefully.
- **12 call sites swept** onto the tenant-aware builder (sentinel, calendar,
  email_search, email_ingest, archive_ingest, renew_drive_channel,
  call_ingest, tools/skip_recurring_instance, briefing, webhook/email,
  api/index calendar-events). Every one now handles the no-creds case
  instead of crashing or reading Danny's calendar.
- **OAuth script (`scripts/update_google_oauth.py --user <name>`)**: token
  exchange now stores the refresh token per user in `user_oauth_tokens` and
  flips `users.google_connected` (dry-run by default).
- **Seeding session (`scripts/seed_user_world.py`)**: `seed_world()` builds
  the tenant's initial graph + settings through the SAME tenant-scoped
  primitives the runtime uses — `create_graph_node_with_db_record` (people /
  orgs + Danny-edges) and `create_task_direct` (initial board) — plus the
  user_settings upsert (context, domains, personal_orgs, timezone) and
  `onboarding_state = 'seeded'`. Importable so the gate can test it.
- **Post-review fixes (critical):**
  - **Home-feed briefing cache is now tenant-scoped** — the Redis key
    previously ignored the tenant, so with 2+ users tenant B could be
    served tenant A's cached briefing (silent cross-tenant leak, the exact
    class M3/M4 exist to kill). `_briefing_cache_key()` namespaces by
    `get_tenant()`; invalidation uses the same scoped key.
  - **`_ensure_danny_edge` now links the tenant's OWN root person** (their
    name via settings) instead of hardcoding "Danny" — a new tenant's seed
    no longer materializes a "Danny" node in their graph (fallback to the
    classic label only for legacy unscoped runs).
  - Seed tasks get a deterministic `dedup_key` (user + title) — re-running
    the seed after a partial failure no longer duplicates tasks.
  - `clear_google_creds_cache()` + call from `update_google_oauth.py` — an
    updated token takes effect without a container restart.
  - `sync_to_calendar` uses the per-tenant timezone instead of hardcoded
    Asia/Kolkata.
- ~~**Known deferred**~~ **Done (M6)**: `archive_ingest`'s `ENTITY_MAPPINGS`
  is no longer a module-level import-time constant — `get_entity_mappings()`
  reads the per-tenant `core_config` row at call time, falling back to
  `DEFAULT_ENTITY_MAPPINGS` (Danny's full mapping). Tenant #1's rich row is
  seeded by `scripts/seed_tenant1_m6_config.py` (8 rows incl. entity_mappings).

**Verification — `scripts/verify_m5_onboarding.py` (14 gates):**
- schema on copy DB (user_oauth_tokens + users.google_connected)
- per-tenant credential resolution: A gets A's token, B without token →
  None (no env leak), per-user cache keying, legacy env fallback intact
- None-safe services: get_cached_service + sentinel calendar skip
- OAuth script targets --user and writes the per-user rows
- seed_world flow: counts, settings write with uid, onboarding_state=seeded
- Plus: py_compile + ruff clean on all changed files; 47 unit tests green;
  M3 (both gates) + M4 (12 gates) all still pass.

### M6 — Cost controls & telemetry (3 days)
- Per-tenant rate limits (LLM calls/min), per-tenant daily LLM budget, spend telemetry surfaced
  (`core/lib/telemetry.py` gains owner_id on observations — part of M3 sweep; dashboard here).
- Exit: you can see cost-per-user per day/week; runaway tenants get capped automatically.

## 7. Cron design (unchanged infra, fanned out)

Same cron-job.org account, same 3 jobs, same endpoints + `PULSE_SECRET`. Each job loops all tenants
internally. Bonus: the 5-min sentinel pings keep the Supabase free-tier project from pausing (if the
project stays on free) — external cron counts as activity.

## 8. Push & Google per tenant

- **Push:** one Firebase project; `device_tokens` gains `owner_id`; `send_push_notification(user_id)`
  targets that user's tokens only.
- **Google:** `user_oauth_tokens` table (user_id, provider, refresh_token, scopes, updated_at).
  `google_service.py` reads per-tenant token instead of env. Manual per-user OAuth remains the one
  human step during onboarding (10-minute call, existing `update_google_oauth.py` flow).

## 9. Testing & validation

- Keep existing suites green: `pytest tests/unit tests/sim tests/clusters`, `scripts/run_full_uat.py`.
- **New: tenant-isolation test suite** (`tests/tenants/`): user A cannot read/write user B rows
  (direct table reads, RPCs, retrieval, graph, pushes, core_config); cross-tenant n-gram collision
  test (mirror `test_ngrams.py`); unauthenticated key rejection; per-tenant settings fallback.
- Migration dry-runs on DB copy with [TENANT-TEST] prefixed rows + cleanup (mirror UAT hygiene).

## 10. Rollout waves

1. **Cutover:** Danny migrates on the branch → green → one deploy. He keeps using the system daily
   (permanent regression test).
2. **Wave 1 (3 users):** invite → onboarding call (Google connect + seed session) → 30 days of
   cohort data. Gates: onboarding completion (wk 1), unprompted use (wk 2), ≥4–5 active + "it caught
   something I'd have missed" (wk 4).
3. **Wave 2 (7 more):** same flow, runbook proven.
4. **50:** pure scaling — no architecture change; per-tenant LLM model routing + RLS hardening +
   billing become the next product iteration.

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cross-tenant data leak | Scoped builder + isolation test suite first; RLS as enforced second layer |
| Danny's brain breaks during rewrite | Branch + DB-copy migration + briefing-equivalence gate + git rollback |
| Retrieval/graph collisions across tenants | owner_id on nodes/edges + filters; dedicated contamination tests |
| Shared blast radius (one Modal app) | Per-tenant rate limits (M6), per-tenant queues, Modal concurrency |
| Free-tier Supabase limits (RLS, scale) | Works for pilot; move to Pro ($25/mo) when RLS/scale needs it |
| Cold start for strangers | M5 onboarding is in the sequence, not after; first cohort IS the pilot |

## 12. Out of scope — later

- Per-tenant LLM model routing & API keys (user's note — deferred)
- Supabase Auth login in the app (replaces raw API key)
- RLS policy hardening pass
- Billing, plans, analytics dashboards for investors
- iOS app, web dashboard parity

## 13. Open decisions

- User onboarding: admin-created invites vs self-serve signup (invites first, obviously)
- Telegram: keep as optional per-tenant channel later vs remove entirely
- Supabase: stay on free tier for pilot vs Pro upfront (Pro only needed for RLS/scale)
