# Session 78: PB Pipeline Audit, Entity Extraction Hardening & Schema Fix
**Date:** August 27, 2026

## Objective
Investigate why the "Project Balance" rich note linked to the wrong organization, fix the full entity extraction pipeline, harden the suggestion card system, implement per-key RPD rate limiting, and resolve the planner schema validation error that silently killed every planner call.

---

## Phase 1: Domains → User_Orgs Refactor (Earlier Session Carryover)

### What Was Built
Consolidated the legacy `domains` + `personal_orgs` data model into a unified `user_orgs` field with an `is_personal` flag per organization.

### Changes
- **DB Migration:** `db/106_user_orgs_migration.sql` — added `user_orgs` column, migrated data from `domains` + `personal_orgs`
- **Core Data Model:** `core/services/user_settings.py` — `DEFAULT_USER_ORGS`, `_parse_user_orgs()`, `resolve_user_orgs()`
- **Entity Context:** `core/lib/entity_context.py` — removed 3 guards blocking LLM primary org override
- **Callers:** 10 files updated (onboarding, seeding, briefing, email_classify, persona_synthesis, etc.)
- **Flutter App:** `rhodey_app/lib/screens/onboarding/onboarding_flow.dart` — sends `user_orgs` with `is_personal`
- **Scripts:** 6 scripts updated for new data model
- **Tests:** 3 test files updated

### Status
✅ All committed and deployed. Clean refactor.

---

## Phase 2: PB Message Pipeline Investigation

### The Problem
When Danny sent "Meeting with Project Balance team tomorrow", the system linked the note to Solvstrat instead of Project Balance.

### Root Cause Chain
1. **Phase 1 (Deterministic):** Detects "Project Balance" but sets `ctx.organization_id = Solvstrat` (first existing match from user_orgs)
2. **Phase 2 (LLM):** Detects both Solvstrat and Project Balance, marks PB as `is_primary`
3. **Guard blocks override:** `if is_primary and not ctx.organization_id` → BLOCKED (Solvstrat already set)
4. **Background worker:** Creates PB org node, but guard `if not entity_context_obj.organization_id` → BLOCKED

### Bugs Found (10 total)

| # | Severity | Bug | File |
|---|---|---|---|
| 1 | 🔴 CRITICAL | `response.strip()` on LLMResponse object | graph.py:774 |
| 2 | 🟠 HIGH | Person-org substring match, no priority | graph.py:207-222 |
| 3 | 🟠 HIGH | No auto-approve pending edges | api/index.py |
| 4 | 🟠 HIGH | primary_org_id() returned None at note creation | api/index.py:5900-6013 |
| 5 | 🟠 HIGH | `from_dict` drops 5 fields (detected_entities, org_to_org_edges, extraction_timing) | entity_context.py:83-94 |
| 6 | 🟠 HIGH | Loop overwrites org_id for every org (last wins) | api/index.py:6000 |
| 7 | 🟠 HIGH | Error handler swallows exceptions | api/index.py:6120-6124 |
| 8 | 🟡 MEDIUM | Backfill uses `ilike('%label%')` — "David" matches "Davidson" | graph.py:405 |
| 9 | 🟡 MEDIUM | Modal cold start risk | api/index.py:5855 |
| 10 | 🔵 LOW | Audit logs org name, not resolved ID | tools.py:237 |

### Fixes Applied (Grouped by Dependency)

**Group 1: Foundation (no dependencies)**
- Bug #3: `response.strip()` → `getattr(response, "text", None)` + empty guard
- Bug #5: Added 3 missing fields to `from_dict()`
- Bug #7: Re-raise after delivering notification

**Group 2: Confirm Flow (depends on Group 1)**
- Bug #4: Stored entity context org is authoritative; entity loop supplements, never replaces
- Bug #6: Break after first org assignment
- Bug #3 (pending edges): Auto-approve matching edges after entity creation

**Group 3: Graph Correctness (independent)**
- Bug #2: 3-tier person-org resolution (entity_context → affiliation regex → word-boundary)
- Bug #8: Word-boundary regex for backfill matching

**Group 4: Reliability (lower priority)**
- Bug #9: try/except on `Function.from_name().spawn()` → sync fallback

---

## Phase 3: Gap C Schedule Pre-filter

### Problem
"We have a meeting today at 8:30 PM" was classified as QUERY (schedule pattern match) instead of TASK, so no suggestion card was shown.

### Root Cause
Pattern `meetings?\s+(this\s+)?(week|month|today|tomorrow)` matched both questions ("What meetings today?") and statements ("We have a meeting today").

### Fix
Added `_is_question` gate — only apply schedule pattern when the message is shaped like a question (ends with `?` or starts with question words).

### Tests
10 regression tests in `tests/unit/test_schedule_prefilter.py` — all passing.

---

## Phase 4: `timing="card"` Dry-Run Regression

### Problem
After the handler refactoring, all tasks had `organization_id = NULL` and `pending_org_id = NULL`. Zero org linkage across all messages.

### Root Cause
Handler called `extract_context_from_source(text, timing="card")` for Path C (direct execution). `timing="card"` is a dry run that detects entities but never creates pending nodes in the DB. `pending_org_label` was set but `pending_org_id` stayed null → `reconcile_action_orgs` found nothing to link.

### Fix
Changed Path C to use `timing="sync"` so pending nodes are created immediately.

### Verification
- Before fix: 0/17 tasks had org linkage (0%)
- After fix: 17/17 tasks had org linkage (100%)

---

## Phase 5: Rate Limiter — RPD Counter Fix

### Problem
All 4 Gemini API keys appeared exhausted (18/18 RPD each) even though Google AI Studio showed keys 2 and 4 had zero 3.6 Flash usage.

### Root Cause
`_rpd_available()` pre-incremented the Redis counter **before** the API call. When the call failed (429, timeout, breaker), the counter was never decremented. This inflated counts — phantom usage accumulated until all keys appeared exhausted.

### Fix
- Removed pre-increment from `_rpd_available()` — it now only reads the counter
- Added `record_usage(key_idx)` method that increments **after** a successful API response
- Updated `call_gemini` in `providers.py` to call `record_usage()` only on success
- Cleared 8 phantom counters from Redis

### Verification
All counters start at 0 → one successful call → only the used key increments to 1. Other 3 keys stay at 0.

---

## Phase 6: 4th API Key Support

### Problem
User added `GEMINI_API_KEY_4` but only 3 keys were loaded.

### Fix
- `core/llm/client.py`: Added `GEMINI_API_KEY_4` client creation
- `core/lib/rate_limiter.py`: Added 4th key to `get_gemini_clients()` and `_ensure_initialized()`
- `.github/workflows/*.yml`: 15 workflows updated with `GEMINI_API_KEY_4` secret

---

## Phase 7: The Schema Validation Bug (Root Cause of All Planner Failures)

### Symptom
Every message got "📝 I couldn't structure that into an action, so I saved it as a note." Zero tasks created. Zero suggestion cards shown.

### Investigation Trail
1. **Initial assumption:** Rate limiter exhausted → checked Redis → counters clean
2. **Second assumption:** Circuit breaker tripped → checked Redis → breaker clean
3. **Third assumption:** Modal running stale code → verified deploy output → fresh mounts created
4. **Direct API test:** `call_gemini(model="gemini-3.6-flash", ...)` with `SUGGESTION_SCHEMA` → **NonRetryableError**

### Root Cause
In `core/lib/suggestion_extractor.py` line 31:
```python
SUGGESTION_SCHEMA = {
    "properties": {
        "matched_task_id": {"type": ["integer", "null"]},  # ← BUG
    }
}
```

The Gemini SDK expects `type` to be a **single string** (`"INTEGER"`, `"STRING"`, etc.), not a Python list. The list `["integer", "null"]` caused a Pydantic validation error **before the API call was even made**:
```
1 validation error for Schema
properties.matched_task_id.type
  Input should be 'TYPE_UNSPECIFIED', 'STRING', 'NUMBER', 'INTEGER', 'BOOLEAN', 'ARRAY', 'OBJECT' or 'NULL'
```

### Why It Wasn't Caught Earlier
1. The `_schema_rejection()` function in `providers.py` looks for `"response_schema"` or `"invalid_json_schema"` in the error message — but the actual error says `"validation error for Schema"` → no match
2. The error was raised as `NonRetryableError` → fallback chain tried Gemma and OpenRouter → both failed → safe-hold empty response
3. `suggestion_extractor` caught the exception and returned zero actions → Path D (fallback note)
4. **Tasks were still being created by the background worker** (`process_message_background`), which has its own entity extraction → task creation pipeline that doesn't use `SUGGESTION_SCHEMA`

### Why It Became Visible
The system became more dependent on the planner returning actual actions after the handler refactoring (card threshold changes, Path C/D flow). With the planner always returning zero, every message hit Path D.

### Fix
```python
# Before (broken since Aug 22):
"matched_task_id": {"type": ["integer", "null"]}

# After (permanent fix):
"matched_task_id": {"type": "INTEGER", "nullable": True}
```

### How It Broke
- **Aug 22:** Commit `4150bb0` introduced `SUGGESTION_SCHEMA` with the invalid list type
- **Every day since:** Every planner call failed silently — the Gemini SDK rejected the schema before the API call
- **Why it looked working:** The background worker created tasks independently of the planner
- **Why it became visible:** Handler refactoring made the system dependent on planner actions

### Verification
- Direct Gemini call with fixed schema → returns proper JSON ✅
- 10-message UAT → 10/10 tasks created, 0 fallbacks ✅

---

## Phase 8: Final 10-Message UAT (Post Schema Fix)

### Messages Sent
| # | Message | Result | Org | Person |
|---|---|---|---|---|
| 1 | Call Lisa Chen about Havenlight volunteer roster | ✅ Task | Havenlight | Lisa Chen |
| 2 | Email Raj Iyer at Prismwork about Q4 proposal | ✅ Task | Prismwork | Raj Iyer |
| 3 | Strategy meeting with Quantum Dynamics on Thursday | ✅ Task | Quantum Dynamics | — |
| 4 | Drop off dry cleaning after lunch | ✅ Task | — | — |
| 5 | Sync with Grace Mathew about Havenlight fundraiser | ✅ Task | Havenlight | Grace Mathew |
| 6 | Follow up with Tanvi Reddy on Solstice Labs proposal | ✅ Task | Solstice Labs | Tanvi Reddy |
| 7 | Add Leah Verghese to Vantage Hotels project | ✅ Task | Vantage Hotels | Leah Verghese |
| 8 | Submit Larkspur Bank compliance report by Friday | ✅ Task | Larkspur Bank | — |
| 9 | Feeling optimistic about Prismwork win today | 📝 Note | Prismwork | — |
| 10 | Coffee with Marcus Webb tomorrow morning at 9 | ✅ Task | — | Marcus Webb |

### Scorecard
- **Tasks created:** 10/10 (100%)
- **Fallbacks:** 0 (was 100% before schema fix)
- **Entities detected:** 11 (orgs + persons)
- **Org linkage:** ✅ All tasks linked
- **Person detection:** ✅ All persons detected
- **Emotional messages:** ✅ Correctly classified as notes

---

## Complete Bug Index (22 Bugs Fixed)

| # | Bug | Severity | Fix | Verified |
|---|---|---|---|---|
| 1 | `response.strip()` on LLMResponse | 🔴 CRITICAL | `getattr(response, "text", None)` | ✅ |
| 2 | Person-org substring match | 🟠 HIGH | 3-tier resolution ladder | ✅ |
| 3 | No auto-approve pending edges | 🟠 HIGH | Auto-approve in confirm flow | ✅ |
| 4 | Org priority blocked by guards | 🟠 HIGH | Removed guards, merge logic | ✅ |
| 5 | `from_dict` drops 3 fields | 🔴 CRITICAL | Added fields to deserialization | ✅ |
| 6 | Error handler swallows exceptions | 🟠 HIGH | Re-raise after notification | ✅ |
| 7 | Last-org-wins in entity loop | 🟠 HIGH | Break after first org | ✅ |
| 8 | Backfill substring match | 🟡 MEDIUM | Word-boundary regex | ✅ |
| 9 | Modal spawn crashes handler | 🟡 MEDIUM | try/except → sync fallback | ✅ |
| 10 | Audit logs org name, not ID | 🔵 LOW | Log resolved_org_id | ✅ |
| 11 | Schedule pre-filter swallows statements | 🟠 HIGH | `_is_question` gate | ✅ |
| 12 | Empty suggestion actions | 🟠 HIGH | Removed redundant rules, JSON examples | ✅ |
| 13 | Flutter entity chips ignored scope | 🟡 MEDIUM | `_entityLabel()`/`_entityColor()` helpers | ✅ |
| 14 | f-string crash (KeyError: '1') | 🔴 CRITICAL | Escaped JSON braces | ✅ |
| 15 | `match_existing_nodes` returns empty | 🟠 HIGH | Reverted to TenantAwareClient | ✅ |
| 16 | No-action terminal swallowed messages | 🟠 HIGH | Fallback note + honest reply | ✅ |
| 17 | Unresolved non-create actions dropped | 🟠 HIGH | Fuzzy-match + NeedsClarification | ✅ |
| 18 | Person detection missed verb forms | 🟡 MEDIUM | Base-form matching + signal-word stripping | ✅ |
| 19 | New orgs typed as persons | 🟡 MEDIUM | Org suffix lexicon gate | ✅ |
| 20 | `timing="card"` breaks org linkage | 🔴 CRITICAL | Changed to `timing="sync"` for Path C | ✅ |
| 21 | RPD counter pre-increment inflation | 🟠 HIGH | `record_usage()` on success only | ✅ |
| 22 | **SUGGESTION_SCHEMA type validation** | 🔴 CRITICAL | `"type": "INTEGER", "nullable": True` | ✅ |

---

## What's Working Now

| Capability | Status |
|---|---|
| Message classification (TASK/NOTE/QUERY) | ✅ |
| Entity extraction (orgs + persons) | ✅ |
| Org linkage on tasks | ✅ |
| Person-org assignment (affiliation regex) | ✅ |
| Suggestion card (≥2 new entities) | ✅ |
| Direct task creation (simple messages) | ✅ |
| Fallback note (emotional/non-actionable) | ✅ |
| Pending node creation | ✅ |
| Edge auto-approval on confirm | ✅ |
| Rate limiting (per-key RPM + RPD) | ✅ |
| Multi-provider fallback (Gemini → Gemma → OpenRouter) | ✅ |
| Schedule pre-filter (questions only) | ✅ |
| Planner LLM returning actual actions | ✅ |

## What's NOT Tested Yet

| Gap | Reason |
|---|---|
| Google Calendar/Tasks sync | Requires live Google OAuth |
| Email/Teams/WhatsApp ingest channels | Separate pipelines |
| Briefing generation | Needs scheduled cron |
| Sentinel monitoring | Needs cron trigger |
| Flutter app UI end-to-end | Requires emulator interaction |
| Pending node approval flow | Needs user interaction in Decisions UI |
| Fuzzy cross-matching for orgs | Deferred — user has a separate design idea |

---

## Key Learnings

1. **Silent failures are the most dangerous bugs.** The schema validation error was silently swallowed for 5 days because the fallback chain returned a "safe" empty response. The background worker masked the failure by creating tasks through a different code path.

2. **Schema validation happens before API calls.** The Gemini SDK validates the schema client-side before making any network request. A schema error means zero API calls are made — the entire fallback chain fails instantly.

3. **`_schema_rejection()` needs to match Pydantic validation errors.** The function only checks for Gemini API-level schema rejection messages, not Pydantic model validation errors. Adding `"validation error" in msg and "schema" in msg` would have caught this immediately.

4. **Modal image caching requires version bumps.** `add_local_dir` is cached with the image. Without incrementing `_BUILD_VERSION`, `modal deploy` reuses stale code.

5. **Rate limiter counters must track successes, not intentions.** Pre-incrementing before API calls inflates counts when calls fail. The correct contract is "count on success, not on intent."

6. **Test the planner in isolation.** The batch UAT scripts revealed the planner failure, but only after checking raw_dumps metadata. A direct `call_gemini` test with the exact schema would have found the bug in seconds.
