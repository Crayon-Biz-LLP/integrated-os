# 76 — Documentation Re-baseline: 100% Verified Diff Spec

> **Purpose:** The single verified source of truth for what every documentation
> file in the repo needs — ADD, REMOVE, or no-change. Every claim below was
> checked against the live system on 2026-08-15 (live Supabase schema query,
> code reads, and the full commit history `9b1ec41..HEAD`, 108 commits).
> The last comprehensive doc update was the Jul 27 restructure (`9b1ec41`).

## Method & verification basis

1. **Live schema** — queried `SUPABASE_URL/rest/v1/` (59 tables) + column
   introspection for every table the docs describe.
2. **Code reads** — every feature claim checked against `core/`, `api/`,
   `frontend/`, `rhodey_app/` (e.g., concept nodes: zero references in
   graph.py/graph_rules.py → removed; `core/pulse/proactive.py` does not
   exist; `conversation_threads.active_anchor` is the live anchor column;
   `rhodey:cache:projects` no longer in ContextProvider; `detect_practices`
   is wired at `core/pulse/briefing.py:1136`).
3. **Commit history** — all 108 commits since the restructure mapped to a
   coverage item in this spec (see §9).
4. **Migrations** — db/75 (people/organizations dropped), db/76
   (person_aliases), db/78–91 (tenant scoping, grants, anon revoke), db/98–99
   (clarifier retire), db/100 (decision action ledger), db/101 (5 tables +
   2 RPCs: `entity_briefs`, `project_organizations`, `retrieval_triples`,
   `retrieval_passage_triple_links`, `retrieval_config`, `match_logs`,
   `match_canonical_pages`).

---

## 1. VERIFIED CURRENT — no work needed (beyond index tags)

| File | Why current |
|---|---|
| `product-summary/00-vision-and-mindset.md` | Canonical vision — evergreen by design |
| `product-summary/60-thread-and-memory-architecture.md` | Marked **IMPLEMENTED (Aug 2026)**, shipped-vs-pending map (§6). Only the README index tag "(PROPOSED)" is wrong → fix index |
| `product-summary/61-action-pipeline-hardening.md` | Aug 13, matches current action pipeline |
| `product-summary/62-pulse-outage-rate-limiter-starvation.md` | Aug 13, matches current rate limiter |
| `product-summary/28-clarification-loop-guards.md` | Aug 14, clarifier retirement noted |
| `product-summary/22b-normalized-label-fix.md` | Historical fix, `normalized_label` still live in schema |
| `product-summary/39-notebooklm-sync.md` | CI workflow unchanged |
| `docs/cutover-runbook.md` (Aug 6) | Current multi-tenant cutover record |
| `docs/scheduling.md` (Aug 12) | Scheduler ownership map, current |
| `docs/beeper-messaging-layer.md` (Aug 12) | **APPROVED + PHASE A SHIPPED** |
| `docs/thread-aware-classification.md` (Aug 11) | Phase 1 IMPLEMENTED |
| `docs/test-inventory.md`, `tests/README.md`, `plans/75` | Updated by the test-suite session |
| `CHANGELOG.md` (≤ Aug 14) | Current through Aug 14 — missing Aug 15 only |

---

## 2. CRITICAL — files with verified-wrong claims (fix first)

### 2.1 `product-summary/05-database-schema.md`
**REMOVE (wrong):** "20 tables" list (live = **59**) · `people` table (dropped
db/75) · `pending_graph_nodes` (renamed `pending_nodes`) · `projects` as an
active feature table (kept, dormant — X1 decision) · `project_id TEXT FK`
(it's `bigint`) · `raw_dumps.user_input`/`processed` (now `content`/`status`)
· RLS claims for dropped tables · enum domains referencing dropped concepts.
**ADD:** full 59-table baseline (verified): `decisions` (+ `decision_action_ledger`
db/100), `subsystem_patterns`, `subsystem_telemetry`, `users`, `user_settings`,
`user_oauth_tokens`, `conversation_threads`, `conversation_workflows`,
`conversations`, `pending_nodes`, `pending_graph_edges`, `merge_proposals`,
`org_creation_signals`, `retrieval_passages`, `retrieval_phrase_nodes`,
`retrieval_node_stats`, `retrieval_passage_phrase_links`,
`retrieval_memory_bundle_links`, `retrieval_alias_edges`,
`retrieval_index_runs`, `retrieval_edges`, `retrieval_eval_*`,
`canonical_pages`, `audit_logs`, `system_audit_logs`, `call_recordings`,
`device_tokens`, `email_drafts`, `login_otps`, `llm_spend`, `model_registry`,
`agent_queue`, `app_intelligence`, `awaiting_reply`, `processed_updates`,
`graph_type_overrides`, `pulse_runs`, `memory_cluster_*`, `pending_*_jobs`,
`projects` (dormant) · **owner_id + tenant scoping** (db/78–79, RLS +
service-role model) · db/87–91 grants/anon-revoke · db/101 drop list.

### 2.2 `product-summary/01-executive-summary.md`
**REMOVE:** "Vercel free tier" / "Vercel" (Modal) · "35+ tables" → 59 ·
"~20,000 LOC" (stale) · "Telegram bot (planning deprecation)" (it is the
primary surface) · "brain synthesis at the **organization level**" (orgs
dropped → graph nodes) · "5 node types incl. organization/project" as the
full story (still node types, but the entity model is graph-consolidated).
**ADD:** multi-tenant (M3) · decisions ledger + learning loop (vision #4) ·
test suite (865 pytest + 62 Flutter, fast/nightly gates) · onboarding +
personas · home-screen modes · Modal + Upstash Redis infra.

### 2.3 `product-summary/03-architecture-overview.md`
**REMOVE:** "survives **Vercel** cold kills" (Modal) · "Python 3.12" → **3.11**
· "Telegram deprecation" · `engine.py` as active · "16 state machines".
**ADD:** tenant-aware data layer (facade, `tenant_scope`, owner_id) ·
decisions/learning loop · test gates · channels (Teams/email/call/outlook/
Beeper) · graph-consolidated entity model.

### 2.4 `product-summary/04-backend-frontend.md`
**REMOVE:** "Vercel rewrites"/two-Vercel-projects · "16 state machines" ·
"12 screens" · stale module tree.
**ADD:** module tree with `core/services/db.py` (tenant_aware_client,
tenant_scope, channel_tenant_scope), `core/decisions.py`,
`core/lib/telemetry.py`, `core/lib/decision_features.py`,
`core/services/auth.py`, `core/services/briefing_refresh.py`,
`core/webhook/email.py` + channel modules, `core/skills/backfill_graph.py`,
`frontend/src/app/api/auto-decisions/` routes, pattern-health components.

### 2.5 `product-summary/99-lovable-product-brief.md` (external-facing!)
**REMOVE (verified wrong):** "No multi-tenancy, no login screens. Single-user
by design" (M3 + M11 Google/OTP sign-in shipped) ·
"`https://integrated-os.vercel.app`" (Modal) · "X-API-Key header configured in
Settings" (sign-in auth model) · "user messages not preserved on the surface"
(the app now has chat bubbles + pulse briefing cards) · "Project/People
Auto-Creation" (decommissioned) · "7 life domains SOLVSTRAT/QHORD/…" as
universal (de-personalized per-tenant, M6).
**ADD:** multi-tenant + sign-in, Modal, current screens (onboarding, inbox,
Quick Confirmations, home modes), current endpoints.

### 2.6 `README.md` (root — Aug 5, highest-traffic)
**REMOVE:** "triangular engine" · deprecated `core/pulse/engine.py`
(lines 25, 50) · Telegram-only framing · Gemini-3.5-flash-lite specifics.
**ADD:** Modal, multi-tenant, Flutter app + dashboard, Beeper, test suite &
gates, pointer to product-summary/README.

### 2.7 `product-summary/02-origin-philosophy.md`
**REMOVE:** "There is no multi-tenant architecture. There are no user accounts
(except the single owner)" (M3/M11) · "Janitor workflow runs 4 times daily"
(Janitor removed → sentinel piggybacks).
**ADD:** privacy-by-architecture updated for the tenant model (owner-scoped,
service-role, per-tenant spend caps).

---

## 3. HIGH — verified-stale feature/architecture docs

| File | Verified problem | Fix |
|---|---|---|
| `16-memory-knowledge-graph.md` | "Concept Fluidity" section (concept nodes re-introduced) — **zero concept references in graph.py/graph_rules.py**; "people table has graph_node_id FK" — **people dropped**; triple-graph flow — **retrieval_triples dropped (db/101)**; "Vercel env vars"; "practice" listed as removed type while `practices.py:415` creates `type='practice'` nodes | Rewrite the graph section: no concepts, graph-consolidated entities, current retrieval tables, practice type note |
| `17-canonical-brain-synthesis.md` | Uses **`match_logs` + `match_canonical_pages` RPCs — both dropped by db/101**; `canonical_pages.organization_id FK → organizations` — **organizations dropped** | Update RPCs + FK (→ graph_nodes(id)), keep per-tenant entity mapping (Aug 6 update is good) |
| `18-passive-intelligence.md` | **`core/pulse/proactive.py` / `check_proactive_signals()` do not exist**; anchor stored in "conversations.metadata" — **live column is `conversation_threads.active_anchor`**; "organization_names" cross-domain bridges (orgs dropped) | Remove proactive-signals section (or re-verify where it moved), fix anchor location, de-org the framing |
| `04b-intelligence-tiers.md` | **`rhodey:cache:projects` removed** (live caches: tasks, people, calendar, recent_tasks, organizations, graph_nodes — core/pulse/context.py:81-86); `check_proactive_signals()` gone; Vercel; intent list incl. CLARIFICATION_NEEDED (clarify flow retired plans/73) | Fix cache table + intent list + remove proactive signals ref |
| `13-pulse-engine-compass-personas.md` | Org-name filtering (SOLVSTRAT/CRAYON/QHORD hardcoded — de-personalized M6); `detect_drift(org_name)` (orgs → graph nodes); persona system now coexists with home-screen modes (Jul 29-30) | De-org the framing, link home-mode doc, verify persona list vs current briefing prompt |
| `19-practices-rhythms.md` | **Feature is LIVE** (`detect_practices` wired at briefing.py:1136) — doc mostly accurate | Minor: de-org the entity-routing framing; verify PERSONAL tag handling per-tenant |
| `30-context-registry-truth-boundary.md` | Test-coverage run command `pytest -c /dev/null` is **dead (Phase 4)**; "Vercel serverless" in Phase 9 | Fix run command, Vercel → Modal; strategies table verified current (all 6 configs exist) |

---

## 4. MEDIUM — superseded / partial-stale docs

| File | Problem | Fix |
|---|---|---|
| `10-task-to-project-assignment.md` | Projects **decommissioned** (Jul 28 `f8ef5c6` + X1 decision) | Supersession banner → graph/org model |
| `11-people-project-autocreation.md` | Same + people dropped (db/75) | Supersession banner |
| `25-whatsapp-ingest.md` | **MacroDroid replaced by Beeper** (`cc7648e`, Aug 11) | Supersession banner → `docs/beeper-messaging-layer.md` |
| `29-conversation-threads-and-workflows.md` | Pre-thread-redesign (Aug 5 `7e904c4`) | Supersession banner → `60-thread-and-memory-architecture.md` |
| `31-decision-audit.md` | Pre-ledger `/why` — no mention of `decisions` table/learning loop | Rewrite or supersede → new decisions/learning-loop doc |
| `08b-telegram-commands.md` | Missing callback surface: `undo_auto_*`, `confirm_auto_all`, `pe{}`/`g{}`, `/why` | Add callbacks section |
| `20-email-pipeline.md` | No direction-awareness (Aug 14 `c0e9f7f`); no briefing_refresh | Add both |
| `38-push-notifications.md` | No `briefing_refresh` silent pushes (Aug 11 `229ba75`) | Add |
| `51-action-planner-architecture.md` | Pre-action-pipeline-hardening (Aug 13 `c740892`) | Add hardening section (typed contracts, PATCH semantics, deterministic time, provider shape, learning loop) |
| `15-llm-architecture.md` | No rate-limiter starvation fix (Aug 13 `a862522`) | Add |
| `07-multimodal-classification.md` | No tenant scoping (M3) | Add |
| `48-flutter-app-architecture.md` | No onboarding (M8/M10), personas (M15/18/2B), Quick Confirmations (Aug 12), per-item undo (Aug 14), home modes | Rewrite the app section |
| `21-frontend-dashboard.md` | "Projects" module (decommissioned); missing auto-decisions per-item verify/reject, pattern-health, DASHBOARD_OWNER_ID gate (Aug 9 `0d48760`) | Update module list |
| `23-governance-security.md` | Auth model changed: Google/email-OTP sign-in (M11, Aug 8), self-serve sign-up (M12), per-tenant LLM spend caps (M6, Aug 6), db/87–91 grants + anon revoke, TEST_CHAT_IDS allow-list, Aug 10 security audit | Rewrite auth + security sections |
| `14-infrastructure.md` / `15-recent-enhancements.md` | Jul 27 dates, Vercel | Refresh dates/claims |
| `.speckit/speckit.specify.md` | "as of Jul 20" — pre-M3, pre-test-suite; "22/22 UAT", people↔graph linkage, org/project node framing | Rewrite "Current System State" |
| `.speckit/speckit.tasks.md` (Jul 28) / `speckit.plan.md` (Jul 27) | Pre-M3/pre-test-suite roadmap + "What Must NOT Change" | Re-baseline |
| `docs/architecture-overview.md` (Jul 18) | "16 state machines", "Telegram deprecation", org/project entity linker, no M3/test-suite | Update diagram + file map |
| `docs/webhook-pipeline.md` / `docs/task-lifecycle.md` (Jul 17) | No direction-awareness, tenant scoping, TEST_CHAT_IDS, snooze fields | Update |
| `rhodey_app/README.md` | Pure Flutter boilerplate ("A new Flutter project") | Replace with real app README |
| `session-notes/README.md`, `plans/README.md`, `product-summary/README.md` | Indexes stale (missing 61/62, 69–75, 69/70/72; 60 tagged PROPOSED; "SVG diagrams" claim false; "(NEW)" tags stale) | Rebuild indexes |
| `AGENTS.md` | No test gate (run_tests.py, pre-push, aspects, sandbox contract) | Add test-gate section |
| `CHANGELOG.md` | Missing Aug 15 (db/101, test suite, ledger X2–X5, onboarding fix) | Add Aug 15 entry |

---

## 5. NEW docs to create (7 product-summary + 2 session-notes)

| File | Scope |
|---|---|
| `70-multi-tenant-architecture.md` | M3 facade, owner_id scoping, tenant_scope/channel scopes, MAX_TENANTS, db/78–84, cutover → docs/cutover-runbook |
| `71-decisions-learning-loop.md` | decisions table/lifecycle, decision_action_ledger (db/100), subsystem_patterns/telemetry, compute_pattern_confidence, undo-training (X2), confirm signals (X3), per-item undo |
| `72-test-suite-and-gates.md` | 13 aspects, runner, fast/nightly CI, pre-push, migration replay, leak guard, sandbox contract (TEST_CHAT_IDS, Redis lock, per-run chat, clean-slate) |
| `73-home-screen-modes.md` | home_mode proceed/decide/sprint/catch_up/wrap, vault segmentation, app_intelligence + 20:00 Intel cron, focal item + transparency report, mode-switch feedback |
| `74-tenant-lifecycle-signin.md` | M6 spend caps/credits, M11 Google/OTP sign-in, M12 self-serve sign-up + snooze escalation, M14 token fallback, M8/M10 onboarding + demo |
| `75-persona-layer.md` | M15 vocabulary picker, M18 Layer-3 persona knowledge, Phase 2B per-tenant surfaces |
| `76-app-approval-surface.md` | Quick Confirmations, selection-mode batch approve/reject, Beeper reply flow, priority buckets, draft editing, voice-rendered acks (ExecutionResults), per-item undo UI |
| `session-notes/73-test-suite-session.md` | Aug 15: suite build + ledger X1–X5 + bugs found |
| `session-notes/74-aug10-14-wave.md` | Aug 10–14: security audit, action hardening, beeper cutover, quick confirmations, direction-awareness, per-item undo |

---

## 6. Supersede / archive ledger

- **Archive tag** (keep file, banner only): `10`, `11`, `25`, `29`, `31`.
- **Replace content**: `05`, `01`, `03`, `04`, `48`, `99-lovable`, root `README`.
- **Delete**: none (everything has historical value) — except `rhodey_app/README.md`
  boilerplate → replace, not delete.

---

## 7. Execution order

1. **Indexes** (3 READMEs + root README) — navigation first.
2. **Factual-critical** (§2): 05, 01, 03, 04, 99-lovable, 02, root README.
3. **Supersede banners** (§4/§6): 10, 11, 25, 29, 31.
4. **High-stale rewrites** (§3): 16, 17, 18, 04b, 13, 30.
5. **New docs** (§5): 7 product-summary + 2 session-notes.
6. **Medium refresh** (§4): 08b, 20, 38, 51, 15, 07, 21, 23, 14/15, .speckit, docs/*.
7. **AGENTS.md + CHANGELOG + session-notes.**
8. **Final sweep:** grep for Vercel (19 files), MacroDroid, `-c /dev/null`,
   `engine.py`, "22 scenarios", "16 state machines" — must be zero hits.

---

## 8. Definition of done

- `grep -rli "vercel" product-summary/ docs/ README.md AGENTS.md .speckit/*.md` →
  zero hits (except historical docs explicitly marked as such).
- Every file in §1 unchanged; every file in §2–§4 has a dated "last verified"
  line; every §5 doc exists and is indexed.
- All three README indexes list every file in their directory.
- `plans/76` superseded by the actual doc updates (close when §7 all done).

## 9. Commit-coverage cross-check (108 commits → spec)

| Commit wave | Coverage in this spec |
|---|---|
| Jul 27–28: Vercel→Modal sweep, projects→orgs removal, CHANGELOG, learning-loop fixes | §2.1–2.6, §4 (10/11), §2.2 |
| Jul 29–30: home-mode system, app_intelligence, focal item, transparency report | §5 (73-home-screen-modes) |
| Aug 1–2: voice rewrite, Telegram-independent reply path, home feed | §4 (48), §5 (76) |
| Aug 5: thread lifecycle redesign | §4 (29 supersede), §1 (60) |
| Aug 6–9: M0–M18 multi-tenant, onboarding, sign-in, persona | §5 (70, 74, 75), §2.7, §4 (23) |
| Aug 8–14: beeper, quick confirmations, direction-awareness, per-item undo, hardening, pulse fixes | §5 (76), §4 (20/25/38/51), §1 (61/62) |
| Aug 10: security audit, pulse-cron | §4 (23), §3 (30) |
| Aug 15: db/101, health fixes, test suite, ledger | §2.1 (schema), §5 (72), §1 (CHANGELOG ≤14) |
| Index/README staleness throughout | §4 (indexes), §2.6 (root README) |

**Zero uncovered commits after this mapping.**
