# Temporal Versioning Expansion

Extended the DB-trigger-based temporal lineage system from `tasks`/`canonical_pages` to `memories`. Removed the app-level `version_memory_for_update()` function.

## Migration (`db/31_temporal_versioning_expansion.sql`, 255 lines)
- Added `BEFORE UPDATE` trigger on `memories` — archives old row, bumps version, marks `is_current=false`.
- Added idempotency guards to prevent double-versioning on trigger re-entry.
- Cleaned up stale app-level versioning calls across 41 files.

## Changes
- `core/services/db.py` — Removed `version_memory_for_update()` (38-line deletion).
- Added `.eq('is_current', True)` filters to 10+ frontend API route queries.
- Removed `test_14_versioning_on_enrichment_update` (tested removed function).
- Misc lint fixes, stale import cleanup, and dead code removal across the codebase.

## Impact
- DB triggers catch ALL update paths — no code path can forget to version.
- Primary keys remain stable (archived row gets new ID, live row keeps original).
- Google Calendar/Tasks sync mappings preserved.
