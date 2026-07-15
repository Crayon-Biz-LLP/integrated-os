# process_single_dump Refactoring

Major refactor of the capture-to-task pipeline. Extracted core processing logic into a shared module, simplified calendar event creation, and built a comprehensive test suite.

## Changes

- **New module**: `core/lib/process_input.py` — centralized `process_single_dump()` and `_run_post_capture_enrichment()`.
- **Calendar events**: Simplified by funneling through existing task workflow instead of direct Google Calendar API calls. Removed 66 lines from `core/services/google_service.py`.
- **Dispatch cleanup**: `core/webhook/dispatch.py` — removed 200+ lines of duplicated routing logic.
- **Workflows simplified**: `core/webhook/workflows.py` — removed 66 lines of dead branches.
- **Tests**: *(Deleted — superseded by Action Planner in Phase 52)*
- **16 files changed**, 1,382 insertions, 528 deletions.

## Impact
- Single path for task/calendar creation reduces duplicate code
- New test suite catches regressions in the enrichment → workflow → task pipeline
- Calendar event creation no longer bypasses task workflow
