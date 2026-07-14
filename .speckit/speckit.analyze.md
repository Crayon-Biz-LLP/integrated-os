# Rhodey OS — Cross-Artifact Consistency Analysis
> Identifies contradictions between Constitution, Spec, Plan, and current codebase.

---

## Contradictions Found

### C-001: RESOLVED — Constitution P2 (Atomic State Transitions)
**Status**: ✅ All raw_dumps state transitions use `staged → processed/embedding_failed` pattern. No record marked `completed` before downstream write confirmed.

### C-002: RESOLVED — Constitution P1 (Zero Silent Failures)
**Status**: ✅ All exceptions logged to `system_audit_logs`. `except: pass` eliminated from all production code.

### C-003: RESOLVED — Constitution P6 (Dead Letter Before Discard)
**Status**: ✅ `dead_letter_queue` table exists. Failed records after 3 retries go to DLQ.

### C-004: RESOLVED — Constitution P10 (System Health Observable)
**Status**: ✅ Janitor heartbeat runs every 30 min. Stalled pipeline records alert via Telegram.

### C-005: RESOLVED — Architecture Plan embedding path
**Status**: ✅ `raw_dumps` staged immediately, embed asynchronously via background processor.

### C-006: RESOLVED — Constitution P5 (Literal Fidelity)
**Status**: ✅ Classification prompt verified: "Use Danny's exact words as the title. Do not rephrase or improve."

### C-007: RESOLVED — SPEC-009 (Simulation Tests)
**Status**: ✅ All 5 simulation suites completed with 32 tests passing. SPEC-009 marked COMPLETED.

### C-007: RESOLVED — SPEC-005 (Backfill)
**Status**: ✅ Backfill ran after atomic pipeline was verified stable.

---

## Items Confirmed Consistent

- ✅ Constitution P4 (IST timezone) — `core/pulse/engine.py` uses `pytz.timezone('Asia/Kolkata')` in all time-aware logic
- ✅ Constitution P9 (entity routing stealth) — entity field is in JSON, not in Telegram receipt text
- ✅ Constitution P7 (fail closed on auth) — `PULSE_SECRET` check returns 401 before any processing
- ✅ Plan: Supabase as single store — confirmed in all files
- ✅ Plan: Telegram as alerting channel — confirmed in core/pulse/engine.py and webhook receipt logic
- ✅ P11 (canonical imports) — all new code uses shared service imports
- ✅ P12 (graph HITL) — all nodes and edges through pending approval
- ✅ P13 (no abstract concepts) — concept nodes fully purged from codebase

---

## Open Risks (Re-evaluated Jul 2026)

| Risk | Severity | Status |
|---|---|---|
| Outlook OAuth2 token refresh fails silently | HIGH | Still no retry/alert on token expiry — needs addressing |
| Graph Edge Expiry (no last_confirmed_at) | MEDIUM | Deferred — edges older than 90 days may be stale |
| Email ingest GitHub Action timeout-minutes | MEDIUM | ✅ Resolved — now has explicit timeout |
| `Design.md` missing from repository | LOW | Root design document was deleted; consider recreating or formalizing product-summary as replacement |
| Flutter APK signing keys management | MEDIUM | CI signs APKs; key rotation/backup not documented |
| RPC UNION type mismatch recurrence | HIGH | `get_context_for` had NULL::uuid vs text mismatch (fixed twice — Jun 25, Jul 7). UNION-heavy SQL functions need schema-level guards |

