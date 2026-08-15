# Integrated-OS Agent Guide

## ⭐ Product Vision — Read First (Non-Negotiable)

Before ANY work on the product — new features, bug fixes, UI changes, prompts,
architecture decisions, or migrations — read the canonical vision document:

> **`product-summary/00-vision-and-mindset.md`**

**The one-line vision:** *A Chief of Staff in your pocket that knows your world,
exercises judgment about what matters now, learns from every decision you make,
and makes you feel understood — so you can do the work that matters.*

Every design/implementation decision must satisfy the evaluation criteria from
that document:

1. **Does this make Rhodey help the user — not the other way around?**
2. **Does this reduce decision fatigue at the front door?**
3. **Does this respect "when to show what" — judgment over volume?**
4. **Does this deepen Rhodey's understanding and its learning loop?**
   (Every user decision — approve/reject/snooze/correct — must persist AND
   train Rhodey. A "Not now" that silently resets is a trust-breaker.)
5. **Does this make the user feel understood?**

Known anti-patterns (dashboard trap, hedged screen, lie buttons, passive vault,
static stage, chatbot passivity, surfacing without learning) are listed in the
vision document — if a change resembles one, it needs rethinking.

## ⚠️ User Operating Rule (Non-Negotiable)

The user has set a strict operating rule for this project:

1. **Do only what the user asks for** — nothing more, no matter how
   obviously helpful or "clearly next" an action seems.
2. **Check with the user before proceeding with any fix** — present the
   plan and the implementation approach, and get explicit approval BEFORE
   making code changes or taking actions.
3. **Do not proceed otherwise.** If a task implies follow-on work the user
   didn't ask for, stop and ask instead of doing it.

When in doubt: stop and ask. A question costs seconds; an unrequested
change costs trust.

## Project Overview
FastAPI-based executive command system deployed on Modal (Python 3.11+). Multi-tenant by design (`owner_id` scoping, OTP/Google sign-in). Processes Telegram/email/Teams/WhatsApp (Beeper) into tasks, syncs with Google Calendar/Tasks, sends AI-generated briefings, and runs a Flutter app (rhodey_app) with a decisions ledger + learning loop. Full verified reference: `product-summary/`.

## Codebase Discovery Workflow

**Use codebase-memory / graph search (`search_graph`, `trace_path`, `get_code_snippet`) as the primary discovery path for all structural questions.** This includes: finding functions, classes, routes, variables; tracing callers/callees; understanding data flow; discovering dependencies; and impact analysis.

Use **grep/ripgrep only as a fallback** when:
1. The index is stale or unavailable
2. The question is a literal text-search problem (string literals, error messages, config values)
3. The graph/index cannot resolve the file or relationship

For non-code files (Dockerfiles, shell scripts, configs), grep/glob remain the primary tool.

## Root Cause Investigation Procedure (Non-Negotiable)

Before applying any fix, follow this procedure step by step. Do NOT skip steps. Each step ensures the fix targets the root cause, not a symptom or a wrong assumption.

**Enforcement**: The `.githooks/commit-msg` hook rejects any commit that lacks a `Root Cause:` line. The 4W1H documentation (Step 10) feeds this line. There is no way to commit a fix without documenting its root cause — the hook enforces it, the procedure defines it, and the `diagnose` skill provides the workflow for complex bugs.

### Step 1: Read the error traceback exactly
- Note the exact error message, error code, file path, line number, and column.
- Note which function/module the error propagates through.
- Do NOT assume you know the error from the message alone — read the full traceback.

### Step 2: Read the failing code
- Open every file in the traceback. Understand what each line does.
- For SQL errors embedded in Python (RPC calls, raw queries), also fetch the actual SQL from the database (`pg_proc.prosrc` for functions, or run the query directly).

### Step 3: Verify the schema
- For database errors (type mismatches, constraint violations, etc.), query `information_schema.columns` for EVERY table and column involved. Do NOT assume column types from name conventions — verify them.
- Sample actual data from the columns in question to confirm your type assumptions.

### Step 4: Trace every column pair in a UNION
- When the error involves UNION/UNION ALL, list every column position in both sides of the UNION.
- For each position, verify: source table column type vs anchor expression type.
- The mismatch is ALWAYS at one specific position. Find it.

### Step 5: Reproduce the error directly
- Call the failing SQL function or query from `supabase_execute_sql` with real parameters.
- Confirm the error matches the original traceback. This is the only way to be certain you've identified the right root cause.

### Step 6: Check git history
- Search for when and why the code was introduced (`git log -p`, `git log -S`).
- Read the commit message to understand original intent. The fix must preserve that intent.
- If the code was created outside version control (SQL editor, etc.), trace when the linked Python/JS code was committed.

### Step 7: Check for sibling callers
- Before modifying shared code (RPCs, utility functions), grep every caller. Patching only the path the error reports may leave sibling callers broken.
- Verify that none of the sibling callers depend on the buggy behavior.

### Step 8: Verify no other UNIONs or type mismatches exist in the same query
- Check the entire query for other potential type mismatches or structural issues.
- For RPCs with overloaded functions, verify which overload is actually called by the application.

### Step 9: Propose the fix
- Only after all 8 preceding steps confirm the root cause.
- The fix must be the smallest change that addresses the root cause — NOT a workaround or symptom patch.

### Step 10: Document the 4W1H
Before committing, write the Root Cause documentation following the 4W1H format. This goes in the commit message body:

```
Root Cause: <why the bug happened, not what you changed — the chain of events that led to the faulty state>
What:       <what the fix does at the code level>
Where:      <which files, which functions, which line ranges>
When:       <reproduction conditions — what input, what state, what sequence triggers it>
How:        <how this fix prevents recurrence — not just "fixed it" but why it won't come back>
```

- The `Root Cause:` line is **enforced by the commit-msg hook** — the commit will be rejected without it.
- The other 4 fields are strongly recommended for any non-trivial fix.
- The `Root Cause:` line feeds into the commit message body, making every commit searchable by root cause later.
- If the fix is purely additive documentation or config, the root cause can be "N/A — docs/config update" to satisfy the hook.


## Engineering Standards & Claims (Non-Negotiable)

When proposing fixes, making architectural changes, or summarizing completed work, adhere strictly to the following standards of honesty and precision:

1. **Do not overstate safety guarantees.** Distinguish clearly between:
   - *Heavily reduced risk* (e.g., read-before-write without a lock, which leaves a TOCTOU window).
   - *Structurally valid* (e.g., using an external API's extended properties for orphan recovery).
   - *Absolute atomic immunity* (e.g., native DB unique constraints, strict transactional locks).

2. **Differentiate recovery from atomic idempotency.** 
   - A sentinel check combined with an external API read-before-write is a *recovery mechanism*. It is not "race-proof" unless the external API natively enforces uniqueness on the idempotency key during insertion.

3. **Timezone hygiene over fixed offsets.** 
   - "Timezone alignment addressed" requires using timezone-aware objects (e.g., `ZoneInfo("Asia/Kolkata")`) and correctly anchoring to real capture times (like `created_at`). Do not mask time logic with `datetime.now(...)` fallbacks where delayed processing would warp relative time contexts (e.g., parsing "Monday 11am" hours or days later).

4. **Prove behavior, don't just lint.**
   - `ruff check .` proves style and syntax compliance. It does not prove concurrency safety, datetime correctness, or workflow semantics.
   - Claims of "deploy safety" must be backed by documented evidence: execution traces of forced-failure paths, delayed-processing proofs, and verifiable edge-case coverage.

## Test Gate (Non-Negotiable)

Every change must pass the suite before it is considered done. See `tests/README.md` and `plans/75-comprehensive-test-plan.md` for the full contract; the essentials:

- **Runner**: `python3 scripts/run_tests.py` is the ONLY entry point.
  - `--tier fast` (~5 min): L0 + L1 + L2-mock + app — runs on the pre-push hook.
  - `--tier nightly` (~20 min): L2-live → L4 + coverage + leak guard — CI nightly.
- **Aspect markers**: every test module carries one of the 13 aspect markers (`pulse`, `briefing`, `sentinel`, `decision`, `ingest`, `webhook`, `auth`, `calendar`, `email`, `sync`, `retrieval`, `graph`, `app`). New/modified test files MUST carry an aspect marker — `python3 scripts/check_marker_presence.py` enforces this and CI fails otherwise. Run a single aspect with `-m <aspect>`.
- **Live-sandbox contract** (when you run live tiers): suites run as the dedicated Test tenant only; live runs serialize behind the Redis sandbox lock; the session fail-closed leak guard fails the run if any test-marker row leaks to a non-test tenant. Never point live tiers at a production tenant.
- **Quick self-check before pushing**: `ruff check .` + `python3 scripts/check_marker_presence.py` + `python3 -m pytest tests/unit tests/sim -q --no-header -p no:cacheprovider` (hermetic). The pre-push hook runs the fast tier automatically.

## Session Anchored Summary (Trimmed)
For full session-by-session history, see `session-notes/` in the project root. This file previously contained ~1700 lines of session summaries here — they have been moved to `session-notes/` to keep AGENTS.md manageable.

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->