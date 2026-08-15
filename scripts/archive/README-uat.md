# Archived UAT harnesses (plans/75 §13 — "wrap, don't rewrite")

These two harnesses were retired from the gate in Phase 2. The automated UAT
suite is now `tests/uat/run_uat.py` driven by the thin pytest adapter
`tests/uat/test_uat_l4.py` (L4 layer), which runs test-tenant-scoped with
owner-scoped cleanup under `python scripts/run_tests.py nightly --live`.

## run_full_uat.py (archived 2026-08-15)
Interactive standalone harness (layers G1-G10/R1-R4, ~2,158 lines) with a
real `input()` HITL pause (`wait_for_hitl`) — it could never be a CI gate.
Bit-rotted since 08-04; only plans referenced it. If you want a manual
deep-verification playbook, plans/63-comprehensive-user-testing-plan.md is
the current human counterpart.

## diag_s5.py (archived 2026-08-15)
"Same as run_uat.py but with S5 diagnostic wrappers" — redundant with
run_uat.py once that became the L4 source (scenario 17 already runs the
full health check, which is the S5 content). No unique scenarios.

## Replacement contract (the leak shape they carried)
- Old: harness set `os.environ["TELEGRAM_CHAT_ID"]` to impersonate the
  owner chat; cleanup was a pattern-only `ilike` delete with NO owner filter
  — the 08-13 runs left `[UAT]` rows under every tenant's owner_id.
- New: dedicated UAT chat (TEST_CHAT_ID, default 909999999) admitted via
  the webhook's TEST_CHAT_IDS allow-list (default-off fail-closed, plans/75
  §7); every cleanup is `eq('owner_id', test_uid)`; the session leak guard
  now sweeps `[UAT]%` rows too.
