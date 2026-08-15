# Golden artifacts — three classes, one rule

Rule (plans/75 §10, v2.8): **no pytest golden depends on a real tenant's
live data.** Every golden is hermetic — compared against mocked fixtures
(sections rows, graph rows, timezone patches), never a live DB read.

| Artifact | Class | Nature | Consumed by |
|---|---|---|---|
| `briefing_tenant1.txt` · `classify_tenant1.txt` · `planner_tenant1.txt` | **Channel-tenant regression pins** | Tenant #1's pinned OUTPUT shape (Danny's exact prompt output at capture time). The `_tenant1` name is accurate — that is the point: these pin HIS output so a prompt refactor that changes it fails the gate. Hermetic: the row/graph/tz used to reproduce them is mocked. | `tests/unit/test_briefing_prompt_golden.py` (briefing) · `test_classify_prompt_golden.py` (ingest) · `test_planner_prompt_golden.py` (decision) + the manual `scripts/verify_m9_*.py` |
| `whatsapp_classify/golden.json` | **Hand-labeled input corpus** | Real chat threads used as INPUT fixtures with hand labels (noise? actionable?) for the deterministic sieve/ask detectors. Not tenant OUTPUT — no re-base applies. | `tests/unit/test_whatsapp_golden.py` (ingest) |
| — (the pytest suites above) | **Test-tenant golden surface** | The §10 pytest gate: byte-identical pin reproduction + neutral fresh-tenant behavior + fail-closed + determinism + no cross-tenant bleed. | pytest, aspect-tagged, runnable via `run_tests.py` |

## Regenerating a pin (channel-tenant class)

A pin goes stale when a prompt/section change is **intentional** (e.g. the
v2.8 re-base: `planner_tenant1.txt` drifted 2 lines after the day-only-task
routing change — "set params.deadline + null reminder_at"). Regenerate from
the current render with the SAME fixtures the test uses, review the diff
like code, and commit the pin + the test together. A stale pin is a
deliberate, reviewed update — never a silent overwrite.

The pytest tests fail loudly when a pin drifts (`assert rendered ==
GOLDEN.read_text()`), which is what turns an unguarded prompt change into a
red CI — this is the M9.x regression protection that used to live only in
manual scripts.

## Golden re-base history

- **v2.8**: `planner_tenant1.txt` regenerated — 2 lines: the "tomorrow"/no-time
  rule now sets `params.deadline` + null `reminder_at` (the day-only-task
  routing behavior). `briefing_tenant1.txt` + `classify_tenant1.txt`
  reproduced clean, no change needed. The plan's earlier §10 wording
  ("re-based off the Test tenant, never tenant1") was corrected: these pins
  are channel-tenant BY DESIGN, hermetic, and never violate the Test-tenant
  principle (plans/75 §7) — no pytest golden reads a real tenant's DB.
