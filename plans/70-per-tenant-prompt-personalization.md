# M9 — Per-Tenant Prompt Personalization (Data-Driven Examples)

**Status:** Proposed · **Branch:** `feat/tenant-prompts` · **Depends on:** M0–M8 (done)

## 1. Goal

Every tenant gets the **same depth of customization Danny has today** — without
losing a single byte of Danny's current behavior.

- **Not** "strip Danny's personalization" — that's what M2 did, and it's done.
- **Not** a blind sweep of prompt files — that's how you break a working OS.
- The task is: **complete the templating job M2 started**, using the
  settings-driven layer that already exists, and add **data-driven examples**
  gated on importance signals the system already computes.

## 2. The core insight

The mechanism already exists. M0–M8 built it:

| Layer | What it holds | Readers |
|---|---|---|
| `user_settings` | `timezone`, `domains` (jsonb), `voice`, `context`, `personal_orgs`, `onboarding_state` | All prompt builders via `resolve_user_name() / resolve_context() / resolve_domains() / resolve_timezone()` |
| `core_config` (per-owner) | `archive_root_label`, `archive_person_labels`, `archive_org_labels`, `email_archive_label`, `entity_mappings`, `github_owner/repo` | archive_ingest, email_ingest, graph.py, backfill |
| `users` | `name`, `telegram_chat_id`, `api_key_hash`, credits | API layer, root-person resolution |
| **Graph (per-owner)** | people, orgs, **canonical_pages, entity_briefs, centrality** | **← the importance signal for data-driven examples** |

**Danny's context is already in his rows.** `DEFAULT_USER_NAME="Danny"`,
his domains (Ashraya/Solvstrat/Qhord/Crayon/Atna), his context line, his
voice, `archive_root_label=Danny`, his entity_mappings — all seeded by
`bootstrap_tenant` / `seed_tenant1_m6_config`. The M2 design principle is the
whole game: **settings → env → neutral fallback, with Danny's row seeded to
reproduce his behavior byte-for-byte.**

Therefore every fix in this plan is **behavior-preserving for Danny by
construction**: swap a literal for a `resolve_*()` slot, and his seeded row
returns the same value. This is verifiable, not aspirational — the same way
`scripts/verify_m2_equivalence.py` already proves it.

## 3. The solution: one mechanism, five slots

Replace remaining literals with **profile slots** fed from the tenant's own
data. Fallback chain everywhere: **tenant's data → neutral generic → never
another tenant's data.**

| # | Slot | Today's literal (Danny's) | Becomes |
|---|---|---|---|
| S1 | **Root-person label** | `"Danny"` in graph.py / backfill_graph / graph_rules | `resolve_root_label()` / `_root_person_label()` |
| S2 | **Example entities** | "Marcus", "Ashraya", "30L recovery", "Call Amma", "Elder Thomas" | `resolve_example_entities()` — top important entities from *their* graph |
| S3 | **Briefing sections** | Work/Home/**Church**/Done/Schedule/Ideas | Section allow-list derived from *their* `domains` |
| S4 | **Timezone** | "IST", "UTC+05:30", `Asia/Kolkata` headers | `{timezone}` from `resolve_timezone()` |
| S5 | **Taxonomy in ingests** | `SOLVSTRAT\|QHORD\|ASHRAYA\|PERSONAL\|CRAYON`, "competitors to Qhord" | "one of the user's domains, or INBOX" |

Plus one app-side slot (S6, separate milestone): **name fallbacks** in
`rhodey_voice.dart` / `adaptive_home_screen.dart` — resolve from settings,
"Danny" only as last-resort fallback.

### 3.1 The example picker (S2) — the design that answers "will it pick an unimportant person?"

No, because frequency is **not** the signal. The system already marks
importance; the picker only consumes those marks:

```
resolve_example_entities(uid):
  1. CANDIDATE GATE   → entities that have a canonical_pages row OR an
                       entity_briefs row (these exist ONLY because the system
                       decided the entity earned permanent memory)
  2. NOISE FILTER     → exclude is_blocklisted_person(), single-word lowercase
                       concepts, NOISE_LABELS (Uncle/The Boys/User cleanup)
  3. IMPORTANCE ORDER → confirmed people with stored roles first (for role
                       examples), then centrality hubs (get_most_connected_nodes
                       RPC — already tenant-scoped via the facade)
  4. CAP              → top 2–3 only
  5. INJECTION GUARD  → a person with no stored role never enters the role
                       example; missing slots fall back to the neutral line
  6. FRESH TENANT     → no canonical pages yet (week one) → neutral examples.
                       Their examples become theirs automatically as the world
                       earns Master Pages
```

For Danny this resolves to Marcus Durai / Ashraya / Sunju — **the same names
that are hardcoded today**, because his graph produced those pages.

### 3.2 Nothing to migrate — the transfer already happened

A common worry is "how do we move today's hardcoded values into the DB?"
**Answer: M2/M6 already did.** Danny's values are all in seeded rows today:

| Danny's prompt literal | Lives in DB (seeded) | Already read by |
|---|---|---|
| Domains (Solvstrat/Qhord/Crayon/Ashraya/Atna/Personal + keywords) | `user_settings.domains` | `resolve_domains()` — classify/email routing |
| Personal orgs (Personal/Ashraya/Chennai…) | `user_settings.personal_orgs` | work-life split |
| Name / context / timezone / voice | `user_settings` | all prompt builders |
| Root label "Danny" | `core_config.archive_root_label` | `resolve_root_label()` / `_root_person_label()` |
| People/orgs incl. roles + canonical pages | `graph_nodes` + `canonical_pages` + `entity_briefs` | S2 queries this |
| Ingest taxonomy | `user_settings.domains` (same list) | S5 replaces literal enum with `resolve_domains()` |
| Archive labels / edge rules / entity mappings | `core_config` rows | archive/email ingests (already config-driven) |

S2/S5 are therefore **read-site swaps, not migrations**: the prompt stops
containing the words and starts querying the tenant's own rows.

**The two genuine new artifacts (seeding steps):**

1. **S3 section mapping** — the grouping *rule* ("Work = Solvstrat+Qhord+
   Crayon+Atna; Home = Personal; Church = Ashraya") exists only implicitly in
   the briefing prompt. M9.3 adds a `briefing_sections` mapping (core_config
   row or derived function): **seeded for Danny to reproduce his exact section
   list**, derived for new tenants from their domains (Home = personal_orgs +
   one section per remaining domain). Same pattern as `archive_edge_rules`.
2. **S2 role sentences** — "Marcus Durai is the Pastor of Ashraya…" is
   **reconstructed** at render time from person + `enrichment.role` + org, not
   stored. Danny's graph has the role → identical render. Tenant without a
   stored role → injection guard falls back to the neutral line (never a fake
   fact).

**Deliberate loss (accepted):** S5 per-domain flavor text ("QHORD: product
GTM, launch (June 2026)") isn't in the DB and won't be — acceptable because
S5 is parked while call/WhatsApp channels remain Danny-unique.

## 4. Milestones (each small, each gated, deployable alone)

### M9.1 — Graph root-label fix (backend, safety-critical — do FIRST)
**Files:** `core/skills/backfill_graph.py` (4 sites: ~L593 `process_memory`,
~L741 `run_backfill`, ~L840 `backfill_emotion_edges`, ~L1143
`backfill_orphaned_node_edges`), `core/pulse/graph.py` (~L1544
`insert_pending_edge("Danny", …)`), `core/lib/graph_rules.py` (~L235 root-label
candidate set).
**Change:** swap literal `"Danny"` → `resolve_root_label()` /
`_root_person_label()`; fallback to no-op when unresolvable.
**Danny gate:** `resolve_root_label()` returns `"Danny"` (seeded
`archive_root_label`) → graph byte-identical. Verify script:
`scripts/verify_m9_1_root_label.py` asserts the resolver returns the same label
under tenant #1's scope and `None` under an empty tenant.
**Why first:** without it, tenant #2's graph gets a phantom "Danny" node and
two maintenance tiers silently skip. No prompts touched → zero LLM risk.

### M9.2 — Data-driven example picker + prototype on classify.py

**Status: HARDENED (design complete, gates specified — code not started).**

**Files:** new `core/services/example_entities.py`
(`resolve_example_entities`), `core/prompts/classify.py`
(`build_classify_intent_prompt` — ROLE_UPDATE example ~L71, role_title ~L42,
org_name ~L43), `core/webhook/classify.py` (~L275 builder call), new
db/89, new `tests/golden/`, new `scripts/verify_m9_2_examples.py`.

**Change:** the ROLE_UPDATE rule's worked example becomes data-driven —
"Example: **{person}** is the **{role}** of **{org}**" — with person/role/org
pulled from the tenant's own graph (their onboarding seed creates people with
roles; `dispatch.handle_role_update` stores `enrichment.role` and
`enrichment.organization_name` on person nodes). Neutral line when absent.

**Why this one:** smallest prompt that proves the mechanism end-to-end before
M9.3 touches the briefing.

#### Hardening spec (the gates — this is what makes it provable like M9.1)

**Step 0 — db/89 (tenant-safe gate tables, lands BEFORE any code).**
`entity_briefs` PK is still global `entity_name` (db/48); db/88 composite
uniques skipped it. Two tenants' sentinels writing a brief for the same name
collide. `db/89_entity_briefs_owner_pk.sql`: drop PK `entity_name`, add
unique `(owner_id, entity_name)`. `canonical_pages` is already safe
(unique `(owner_id, title)`, db/88).

**Step 1 — BASELINE FIRST (before any edit).** Add
`scripts/capture_classify_baseline.py`: under tenant #1 scope, call
`build_classify_intent_prompt(...)` with a fixed sample message, write the
rendered prompt to `tests/golden/classify_tenant1.txt` (committed). This
committed artifact is the byte-diff target for the whole milestone. Without it
the gate is aspirational.

**Step 2 — the picker (`core/services/example_entities.py`).**
`resolve_role_update_example()` — **sync** (mirrors `resolve_user_name()` /
`routing_rules_text()` which `build_classify_intent_prompt` already calls
synchronously), with a **per-tenant TTL cache keyed by owner_id** (resolved
BEFORE the cache lookup — never a global cache, the google_service
cross-tenant lesson), TTL 15 min. Importance gate is **canonical_pages ONLY**
(owner-scoped; `entity_briefs` is dormant — zero Python refs — so the picker
does not depend on it; db/89 stays as hygiene). Total order (byte-diff must
be deterministic):

```
1. CANDIDATES  = canonical_pages rows (owner_id-scoped) + entity_briefs
                 rows (owner_id-scoped)
2. NOISE FILTER → is_blocklisted_person(), single-word lowercase concepts,
                 NOISE_LABELS
3. RANK:
   a. confirmed people with stored role (enrichment.role non-empty)
      ← feeds the ROLE_UPDATE example
   b. centrality hubs (get_most_connected_nodes RPC, limit 3)
   c. tiebreak: label ASC — ALWAYS (same DB state ⇒ same prompt)
4. CAP = top 2–3
5. INJECTION GUARD → person without stored role never enters the role
   example; missing slot ⇒ neutral line (never a fake fact)
6. NEVER-RAISE CONTRACT → any exception (DB down, RPC missing, schema
   drift) ⇒ neutral line; never a crash, never a 500, never another
   tenant's entity
```

Neutral line (static, no names): *"Example: a colleague is now the head of a
client organization → route to the matching domain or INBOX."* Fresh tenant
(no pages yet) ⇒ neutral, with no extra DB round-trip on the miss path.

**Step 3 — the template swap.** `build_classify_intent_prompt()` gains the
example via a new slot (e.g. `role_update_example: str | None = None`,
resolved inside the builder via the picker, or threaded from the caller —
caller is `core/webhook/classify.py:275`, single site). role_title/org_name
field docs (~L42–43) drop "Pastor" / "Ashraya Chennai Central" literals →
neutral wording.

**Step 4 — the gate (`scripts/verify_m9_2_examples.py`), 5 assertions:**

```
1. BYTE-IDENTICAL for Danny — ROLE_UPDATE example line rendered from his
   mocked graph == the line in tests/golden/classify_tenant1.txt; the ONLY
   permitted whole-prompt diffs are the 2 whitelisted cosmetic doc lines
   (role_title/org_name descriptions, neutralized per plan) — any other
   change fails the gate
2. NEUTRAL for fresh tenant — no canonical pages ⇒ neutral line rendered
3. DETERMINISM — picker called twice (cold cache) ⇒ identical output
4. FAIL-CLOSED — mocked DB exception ⇒ neutral line, no raise
5. NO CROSS-TENANT LEAK — cache keyed by owner_id; two tenants resolve
   their own examples; per-owner cache entries distinct
```

**Entity-tag detail (byte-identical lesson):** the original literal renders
`entity=ASHRAYA` (uppercased). `_resolve_entity_for_org()` uppercases the
matched domain name so Danny's example is byte-identical and the entity tag
style is consistent for every tenant.

**Step 5 — deploy gate (same as M9.1):** py_compile + ruff + unit tests +
code-reviewer pass, then deploy and send one real ROLE_UPDATE-style message
through live classify — confirm it still routes `entity=ASHRAYA` with the
same receipt (Danny's behavior unchanged on live).

**Danny gate restated:** his graph produced Marcus's canonical page + role, so
the example renders "Marcus Durai is the Pastor of Ashraya Chennai Central" —
identical to today, proven by Step 1's baseline, not asserted.

### M9.3 — Briefing sections from domains
**Files:** `core/prompts/briefing.py` (section list ~L103–104, URGENT/NIGHT
overrides ~L123–124, "faith" ~L75, "30L recovery" ~L112, "Marcus" ~L113,
Qhord examples ~L237/255), `core/pulse/briefing.py` (pass sections into ctx).
**Design (the simplification):** one **base skeleton for every tenant** —
Schedule · Done · Work · Home · Ideas · Stale Loops — plus an optional
per-tenant **domain-sections list** (explicit config, NOT auto-derived from
domains — auto-derivation would over-generate for Danny: his 5 work domains
would become 5 sections instead of his single combined Work + one Church).
The Work/Home split already derives from `personal_orgs`.
```
core_config row "briefing_sections":
  Danny  → [Church: "Ashraya admin, operations, finance tasks only"]  (seeded)
  Others → []   (empty = base skeleton only)
```
All ~6 "Church" references (section list, Home desc, DATA FIDELITY, URGENT,
NIGHT, framing) collapse into **one generated block** — the resolver builds
"THE BOARD + SECTION RULES + MODE OVERRIDES" as a single injected string.
**Danny gate:** his seeded `briefing_sections` row reproduces
Work/Home/Church/Done/Schedule/Ideas verbatim. Verify:
`scripts/verify_m9_3_sections.py` — rendered prompt diff under tenant #1.
**Bonus:** this is the "content focus" half of the per-tenant briefing options
(a tenant's sections list == their briefing preference; empty list ==
Work+Home only).

### M9.4 — Timezone slot injection (DONE)
**Files:** `core/prompts/planner.py` (TIME FORMATTING RULES block),
`core/prompts/workflow.py` (enrichment prompt tz + ISO offset),
`core/prompts/briefing.py` (evening-phase line in the briefing builder;
HIGH-PRECISION TIME FORMATTING rule in `build_pulse_system_instruction`),
`core/services/outlook_service.py` (`outlook.timezone` Prefer header),
`core/services/google_service.py` (`format_rfc3339` `+05:30` literals),
`core/pulse/briefing.py` (strftime "IST" → `%Z`), `core/webhook/dispatch.py`
(2× `IST_TIMEZONE` → `get_user_timezone()`, 2× "IST" → `%Z`, display site
`now_ist()` → `now_for_user()`), `core/webhook/commands.py` (status footer
label, /urgent ISO offset).
**Change:** the runtime already resolved the tenant tz (M2,
`get_user_timezone()`); M9.4 replaced the literal `IST`/`+05:30` strings in
prompts, rendered output, and service headers with `tz_label()` /
`tz_offset_str()` (new helpers in `core/lib/time_utils.py`, computed from the
resolved tzinfo, IST fallback, never-raise). Danny: Asia/Kolkata →
byte-identical outputs (proven).
**Gate:** `scripts/verify_m9_4_timezone.py` — 17 checks green: Danny's
briefing == the M9.3 golden AND planner == the M9.4 golden (zero diffs);
helpers correct; non-IST tenant (Asia/Tokyo) gets JST/+09:00 in every prompt;
determinism; fail-closed; no cross-tenant leak; Google formatter offsets.
**Baselines:** `scripts/capture_m9_4_baseline.py` +
`tests/golden/planner_tenant1.txt` (captured BEFORE the edit; the briefing
golden from M9.3 is reused). The system-instruction swap is gated by
substring checks (its prompt embeds live context, not a golden).
**Deliberate deferrals (tenant-#2 readiness pass, not M9.4):** `dispatch.py`
internal `now_ist()` sites (L580/659/668/820 — self-consistent cutoffs and
`last_mentioned_at` metadata) stay IST-anchored; migrate to `now_for_user()`
when a non-IST tenant actually onboards. Raw `%Z` sites render the label of
the resolved tz by construction; the nameless fixed-offset fallback (only if
`ZoneInfo("Asia/Kolkata")` ever raised) would render blank — unreachable in
practice, `tz_label()` covers the prompt/display paths that need a label.

### M9.5 — Ingest taxonomy from domains (LOW priority / optional)
**Files:** `core/skills/call_ingest.py` (~L86–98), `core/skills/whatsapp_ingest.py`
(~L25,49), `core/pulse/resources.py` (~L65–69).
**Change:** project enums → "one of the user's domains, or INBOX"; example
strings → neutral or data-driven via S2.
**Verdict:** call/WhatsApp channels are confirmed Danny-unique today — this is
**pure future-proofing, zero current value**. Park unless/until those channels
are fanned out per-tenant.

### M9.6 — App name lines (Flutter, separate APK release)
**Files:** `rhodey_app/lib/voice/rhodey_voice.dart` (~L15,18 "All clear,
Danny"), `rhodey_app/lib/screens/adaptive_home_screen.dart` (~L2003,2012),
placeholders `entities_screen.dart` (~L790 "CrayonBiz LLP"),
`inbox_screen.dart` (~L258 "VP at Qhord").
**Change:** resolve display name from the user's settings (the API already
returns the tenant's name); "Danny" only as last-resort fallback. Placeholders
→ neutral ("e.g. their org").
**Gate:** `flutter analyze` + manual check on tenant #2's APK.
**Deferred product decision (NOT this plan):** APK `applicationId =
com.crayon.rhodey_app` — rebranding changes package identity, breaks FCM
tokens + widget IDs. Separate decision.

## 5. What we deliberately do NOT touch

| Item | Why |
|---|---|
| `messages.danny_decision` column (~30 sites) | Invisible to tenants; rename = live-DB migration for zero user impact. Batch later if ever. |
| `frontend/` web dashboard (Danny-centric graph UI) | Legacy surface, not in the app product path. |
| `user_settings.py` Danny-era **defaults** | Deliberate fallback — the guarantee. Seed rows override. |
| `.github/workflows` IST cron times, `TELEGRAM_CHAT_ID` | Danny's CI/admin notifications — by design. |
| `archive_ingest` journal-sheet mapping (Prophecy/Psalm/Prayer/faith_score) | Danny's personal sheet format — his channel, not a template. |
| Comments, docs, session-notes, test fixtures | Document the migration itself. |

## 6. How Danny's context is preserved (the guarantee, restated)

1. **Relocation, not removal.** Every literal being replaced already has a
   home in Danny's seeded rows (M2/M6). The plan never deletes his values —
   it moves the *read site* from code constant to settings slot.
2. **Default chain keeps his output identical.** `settings → env → neutral`.
   His row is populated → the middle and last links never fire for him.
3. **Per-milestone equivalence gate.** Each M9.x ships with a verify script
   (pattern: `verify_m2_equivalence.py`) that renders the affected prompt /
   resolver under tenant #1's scope and diffs against the pre-change output.
4. **Ordered risk.** M9.1 (no prompts), M9.2 (one example), M9.3 (sections),
   M9.4 (timezone) — prompts are only touched after the resolver layer is
   proven, one slot at a time, each independently deployable and reversible.
5. **Rollback.** Each milestone is its own small commit; reverting one does
   not touch the others. Live verification: run your real pulse briefing
   before/after each deploy and eyeball the diff (same pattern as the
   M0 cutover).

## 7. Sequencing recommendation

1. **M9.1** — land first (protects tenant #2's graph; zero LLM risk).
2. **M9.2** — prototype the example mechanism (the answer to "is data-driven
   doable?" proven on one prompt).
3. **M9.6** — app name lines before tenant #2 installs (she shouldn't see
   "Danny").
4. **M9.3** — briefing sections (also unlocks tenant briefing options).
5. **M9.4** — timezone (cheap while files are open).
6. **M9.5** — park until channels are fanned out.

## 8. Exit criteria

- [ ] M9.1 verify script green on live (tenant #1 graph unchanged)
- [ ] M9.2 `verify_m9_2_examples.py` green (baseline byte-diff for Danny,
      neutral fresh tenant, determinism, fail-closed, no cross-tenant leak)
      + one live ROLE_UPDATE message routes identically
- [ ] M9.6 tenant #2's app never shows "Danny"
- [ ] M9.3 Danny's briefing sections identical; her sections come from her
      domains
- [ ] M9.4 Danny's timestamps identical (`+05:30`)
- [ ] No prompt output regression across one full day of Danny's real pulses
