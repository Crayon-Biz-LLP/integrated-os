# Rhodey OS — Cross-Artifact Consistency Analysis
> Identifies contradictions between Constitution, Spec, Plan, and current codebase.

---

## Contradictions Found

### C-001: CRITICAL — Constitution P2 violated by current code
**Constitution says**: State transitions must be atomic. Never mark complete before downstream write confirmed.
**Current code does**: `raw_dumps.status = 'completed'` is set in a branch that can be reached even after `get_embedding()` raises an exception.
**Resolution**: SPEC-001 (T-006) fixes this.

---

### C-002: CRITICAL — Constitution P1 violated by current code
**Constitution says**: Zero silent failures. All exceptions must be logged to `system_audit_logs`.
**Current code does**: `except Exception as e: print(f"Error: {e}")` — logs to GitHub Actions stdout which expires.
**Resolution**: SPEC-002 (T-002, T-004, T-005) fixes this.

---

### C-003: Constitution P6 violated — no DLQ exists
**Constitution says**: If a record fails after 3 attempts, it goes to `dead_letter_queue`.
**Current state**: No DLQ table exists. Failed records are either silently dropped or left in `completed` with null embeddings.
**Resolution**: SPEC-003 (T-003) fixes this.

---

### C-004: Constitution P10 violated — no Janitor exists
**Constitution says**: If pipeline has not processed a record in 60+ minutes during business hours, alert Danny.
**Current state**: No monitoring or alerting exists on `raw_dumps`.
**Resolution**: SPEC-004 (T-008) fixes this.

---

### C-005: Architecture Plan vs. current code — embedding in webhook path
**Plan says**: Stage immediately, embed asynchronously.
**Current code**: Embedding happens synchronously inside the webhook handler.
**Resolution**: T-006 restructures the flow. Until T-006 is deployed, T-001 patches the immediate failure mode.

---

### C-006: Constitution P5 (Literal Fidelity) — unverified
**Constitution says**: title must mirror Danny's exact words.
**Status**: UNVERIFIABLE from current spec alone. Requires prompt audit on `classify_input()` system prompt.
**Action**: During T-005 audit, review the classification prompt's task title instruction. Confirm it says: "Use Danny's exact words as the title. Do not rephrase or improve."

---

### C-007: SPEC-005 (backfill) depends on SPEC-001 being stable first
**Risk**: If T-007 (backfill) runs before T-006 is deployed and stable, newly backfilled records will go back through the old broken pipeline and potentially re-fail.
**Resolution**: T-007 explicitly lists T-006 as dependency. Enforce this sequencing. Do NOT backfill until the atomic pipeline is live and verified for 48 hours.

---

## Items Confirmed Consistent

- ✅ Constitution P4 (IST timezone) — `core/pulse/engine.py` uses `pytz.timezone('Asia/Kolkata')` in all time-aware logic
- ✅ Constitution P9 (entity routing stealth) — entity field is in JSON, not in Telegram receipt text
- ✅ Constitution P7 (fail closed on auth) — `PULSE_SECRET` check returns 401 before any processing
- ✅ Plan: Supabase as single store — confirmed in all files
- ✅ Plan: Telegram as alerting channel — confirmed in core/pulse/engine.py and webhook receipt logic

---

## Open Risks (Not Yet Specced)

| Risk | Severity | Status |
|---|---|---|
| Outlook OAuth2 token refresh fails silently | HIGH | No retry/alert on token expiry |
| `brain_synth.py` overwrites canonical_pages without versioning | MEDIUM | Temporal lineage spec (T-010) queued |
| Email ingest GitHub Action has no `timeout-minutes` | MEDIUM | Caused the "not acquired by runner" error on May 5 |
| `classify_input()` prompt has no structured JSON schema enforcement | MEDIUM | Add `response_mime_type: application/json` to Gemini call |
| No idempotency on email ingestion (same email processed twice) | HIGH | SPEC not written yet |

