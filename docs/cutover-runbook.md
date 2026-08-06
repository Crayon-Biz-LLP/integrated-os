# Cutover Runbook — m0-tenant-scoping → live

Flips the live system from single-user (Danny, env-configured) to the shared
multi-tenant system (plan `plans/69-multi-tenant-product-plan.md`). Danny
becomes tenant #1. One Modal app, one Supabase project, one set of cron jobs.

**Do not skip preconditions.** This runbook is the "absolute sure" gate — every
step below was validated on the copy DB first.

---

## Preconditions (all must be true before starting)

| # | Check | Command / evidence |
|---|---|---|
| P1 | Branch `m0-tenant-scoping` committed & pushed | `git log --oneline main..HEAD` shows M0–M6 work |
| P2 | All 7 gates green locally | `verify_m2…verify_m7` scripts all print `ALL … GATES PASSED` |
| P3 | Copy DB (`rhodey_restore_test`) migrated with db/78–86 | `psql … -c "\d users"` shows `monthly_credit_usd`, `credit_cycle_day`, `telegram_chat_id`; `llm_spend` exists |
| P4 | `migrate_danny_to_tenant1.py --verify-only` passes on the copy | `✅` on every table |
| P5 | `modal` CLI authenticated, secret `rhodey-os` exists | `modal secret list` |
| P6 | Live DB credentials in `.env` (pooler DSN) — password via `PGPASSWORD` or `--dsn` | `scripts/backup_supabase.py` discovers from `.env` |
| P7 | Danny's per-user API key generated (openssl rand -hex 32) | saved somewhere safe; entered in the app in Step 9 |

---

## The ordering rule (why deploy comes before migrate)

`db/78` drops `core_config_key_key` and makes the PK `(owner_id, key)`.
The OLD code's `on_conflict='key'` upserts would 400 the moment that
constraint is gone. Therefore:

> **The new code MUST be live on Modal BEFORE db/78 lands.** New code on the
> old schema runs in legacy mode (`tenant_mode_enabled()` = False → unscoped,
> byte-identical behavior). Old code on the new schema is broken.

Second subtlety: `tenant_mode_enabled()` caches per process. A container that
starts before db/78 probes `users` missing → caches `False`. After migration
it must be recycled to re-probe. Steps 9–10 handle this.

---

## Step 0 — Fresh backup (safety net, non-negotiable)

```bash
cd /Users/danielyashwant/Antigravity/Integrated-OS
python3 scripts/backup_supabase.py
# → backups/rhodey-full-<ts>.dump  (primary restore artifact)
# → backups/rhodey-public-<ts>.sql
```

Verify the dump is readable (script does this) and note the filename.
**This is the rollback artifact.** Do not proceed if this fails.

## Step 1 — Deploy the new code to Modal (legacy-safe)

```bash
modal deploy infra/modal_app.py
```

The app is `rhodey-os`, secret `rhodey-os`, `min_containers=1`. New code on
old schema = legacy unscoped mode = identical behavior to today.

## Step 2 — Verify the new deploy behaves identically (still legacy)

```bash
# Health (runs the full pipeline health check, fanned out per user — 0 users → once unscoped)
curl -s -X POST https://<your-modal-url>/api/health \
  -H "x-pulse-secret: $PULSE_SECRET" | head -c 400

# A manual briefing — must look exactly like Danny's usual output
curl -s -X POST https://<your-modal-url>/api/pulse \
  -H "x-pulse-secret: $PULSE_SECRET" | head -c 600
```

Both must succeed and look normal. **Stop here if anything is off** — nothing
has been migrated yet; the old system is untouched.

## Step 3 — Apply migrations db/78 → db/86 (in order, one psql session)

`scripts/apply_migrations.py` is a *test* of core_config upsert semantics —
**not** the migrator. Apply the SQL files directly with psql:

```bash
export PATH=/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:$PATH
# Pooler DSN — put password in env, never on the command line
export PGPASSWORD='<your-password>'
DSN="postgresql://postgres.<ref>@<host>:6543/postgres"

for f in db/78_tenant_scoping.sql db/79_rpc_owner_scoping.sql \
         db/80_rpc_owner_scoping_2.sql db/81_rpc_owner_scoping_fixes.sql \
         db/82_rpc_owner_scoping_graph.sql db/83_users_telegram_chat.sql \
         db/84_user_oauth_tokens.sql db/85_llm_spend.sql db/86_user_credit.sql; do
  echo "--- $f ---"
  psql "$DSN" -v ON_ERROR_STOP=1 -f "$f" || { echo "❌ STOP at $f"; exit 1; }
done
```

Schema smoke (must all return rows/columns):

```bash
psql "$DSN" -tAc "select table_name from information_schema.tables where table_schema='public' and table_name in ('users','user_settings','llm_spend','user_oauth_tokens') order by 1"
psql "$DSN" -tAc "select column_name from information_schema.columns where table_name='users' and column_name in ('monthly_credit_usd','credit_cycle_day','telegram_chat_id','api_key_hash') order by 1"
```

> ⚠️ From this moment the DB is multi-tenant-shaped. The app code is new
> (Step 1) so nothing breaks yet — but keep moving; do not leave it half-done.

## Step 4 — Bootstrap tenant #1 (users row + settings + root node + neutral M6 rows)

> Before running, pull Danny's EXACT settings from the copy DB so the
> post-cutover equivalence gate (V1) compares like-for-like:
> `select domains, personal_orgs, timezone, context from public.user_settings
> where user_id = (select id from public.users where name='Danny')` on the
> copy — pass those exact values below.

```bash
python3 scripts/bootstrap_tenant.py \
  --name Danny \
  --email <danny-email> \
  --timezone Asia/Kolkata \
  --domains '<json array from copy DB, e.g. ["Ashraya","Solvstrat","Qhord"]>' \
  --personal-orgs '<json array from copy DB>' \
  --context '<danny one-liner from copy DB>' \
  --api-key '<generated per-user key from P7>' \
  --apply
```

Idempotent: safe to re-run. Creates/updates `users`, upserts `user_settings`,
ensures neutral M6 `core_config` rows, upserts the root graph node.
**Note the printed tenant id** (needed to sanity-check Step 5).

## Step 5 — Migrate Danny's data (DRY-RUN first, then live)

```bash
# 1. Dry-run — reviews per-table totals / untagged counts, writes NOTHING
python3 scripts/migrate_danny_to_tenant1.py

# 2. Review: every table must show tagged/total with no unexpected untagged
#    rows, and the users row must be found. If anything looks off, stop here.

# 3. Live — backfills owner_id everywhere + SET NOT NULL finalize + verify
python3 scripts/migrate_danny_to_tenant1.py --apply
```

`--apply` runs verification after writing: every table `✅ N/N attributed`,
`core_config` dupes `0`, users row present. If any `⚠️` appears: **do not
continue** — investigate, fix, re-run (idempotent).

## Step 6 — Seed Danny's M6 config rows (values live in DB, not fallbacks)

```bash
python3 scripts/seed_tenant1_m6_config.py --user Danny --apply
# email_archive_label, archive labels/edges/root, entity_mappings, github_owner/repo
```

## Step 7 — Persist Danny's Telegram chat id (survives user #2)

The env `TELEGRAM_CHAT_ID` fallback only works while Danny is the *only*
active user. Persist it so his nudges don't silently stop when tenant #2
arrives:

```bash
psql "$DSN" -tAc "update public.users set telegram_chat_id = '<value from .env TELEGRAM_CHAT_ID>' where name = 'Danny';"
psql "$DSN" -tAc "select name, telegram_chat_id from public.users;"
```

## Step 8 — Per-user Google OAuth (move Danny's token out of env)

```bash
python3 scripts/update_google_oauth.py --user Danny --apply
```

Stores the refresh token in `user_oauth_tokens`, flips `users.google_connected`,
and clears the creds cache. Verify: `select name, google_connected from public.users;`

## Step 9 — Point Danny's app at the per-user API key (BEFORE recycle)

In the Android app → Settings → enter the per-user API key from P7
(the app already sends `X-API-Key` and resolves the tenant — no APK change).

⚠️ **This must happen BEFORE Step 10.** Once containers re-probe tenant mode
on, an app still sending the legacy shared key would get NO tenant context
→ the facade fails closed → 500s. A resolved per-user key sets the tenant
under both legacy-cached and tenant-mode containers, so entering it first
means zero outage.

Verify the key resolves against a data endpoint that reads `X-API-Key`
(`/api/health` is cron-gated and will 401 a bare API key — don't use it):

```bash
curl -s "https://<your-modal-url>/api/tasks?limit=1" \
  -H "X-API-Key: <per-user-key>" | head -c 300
```

## Step 10 — Recycle Modal containers (fresh tenant-mode probe)

The warm container from Step 1 cached `tenant_mode_enabled() = False`.
Recycle so it re-probes and sees the `users` table. The deterministic way
is scale-to-zero (a bare `modal deploy` with an unchanged image may reuse
the warm instance):

```bash
modal app stop rhodey-os      # scale to 0; next request spins a fresh process
# or, if you prefer a redeploy: modal deploy infra/modal_app.py
```

Confirm tenant mode is now ON (a request with an unknown tenant key should
resolve to no user; the app's `/api/tasks` with Danny's key must return HIS
data only).

---

## Step 11 — Post-cutover verification (the acceptance gate)

⚠️ **Live-safety note:** `verify_m3_*`, `verify_m4`, `verify_m5` INSERT test
users/rows into the DB they're pointed at — they are **copy-DB gates**, never
point them at production. Re-run them against the copy DB for the final
pre-cutover confirmation; on LIVE use the read-only checks below.

| # | Check | Command |
|---|---|---|
| V1 | Briefing equivalence on LIVE (read-only gate) | `python3 scripts/verify_m2_equivalence.py --dsn "<live DSN>"` — all gates pass |
| V2 | Health + briefing end-to-end | `/api/health` and `/api/pulse` with `x-pulse-secret` (Step 2 commands) — normal output |
| V3 | Isolation gates on COPY (final re-run) | `verify_m3_webhook_isolation.py` + `verify_m3_remaining_isolation.py` + `verify_m4_cron_fanout.py` + `verify_m5_onboarding.py` against `rhodey_restore_test` — all pass |
| V4 | Cost controls | `scripts/verify_m7_cost_controls.py` passes (mock/local); `GET /api/admin/spend?days=7` (Bearer `PULSE_SECRET`) shows Danny's credit overlay |
| V5 | Cron jobs still firing | cron-job.org last-run times fresh after 5m/30m cycles |
| V6 | Danny's daily flows | briefing reads identically; `GET /api/tasks` with his key returns his data; pushes land on his device; Telegram nudges still arrive |
| V7 | No cross-tenant leak (manual, careful) | in psql: insert a `[TENANT-TEST]`-marked user row → with Danny's `X-API-Key`, confirm his reads do NOT include that user's rows → delete the test row |

**Only when V1–V7 are green is the cutover complete.**

---

## Rollback plan (how to get back)

The dump from Step 0 is the primary restore artifact:

```bash
# 1. Redeploy the pre-multi-tenant code (old schema-compatible)
git checkout main && modal deploy infra/modal_app.py

# 2. Restore the live DB from the Step 0 dump
pg_restore --clean --if-exists --no-owner -d "$DSN" backups/rhodey-full-<ts>.dump
```

Rollback = revert the branch (git history) + restore the DB (dump). RLS
policies added by the migrations are the second safety net (service role
bypasses them today; they harden later).

---

## Checklist (print this)

- [ ] P1–P7 preconditions
- [ ] Step 0: fresh backup taken & readable
- [ ] Step 1: new code deployed
- [ ] Step 2: legacy behavior verified (health + briefing normal)
- [ ] Step 3: db/78–86 applied in order, schema smoke OK
- [ ] Step 4: Danny bootstrapped, tenant id noted
- [ ] Step 5: migrate --apply, all `✅`
- [ ] Step 6: M6 config seeded
- [ ] Step 7: telegram_chat_id persisted
- [ ] Step 8: Google token per-user
- [ ] Step 9: app key entered, `/api/tasks` with X-API-Key returns Danny's data
- [ ] Step 10: containers recycled, tenant mode confirmed ON
- [ ] V1–V7 all green
- [ ] Runbook updated in repo after cutover (what actually happened)
