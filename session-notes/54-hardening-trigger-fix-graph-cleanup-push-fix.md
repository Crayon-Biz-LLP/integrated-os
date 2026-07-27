# Part 54: Hardening — Trigger Fix, Graph Cleanup, Push & WhatsApp Fixes

## Overview
Hardening pass fixing a production crash (task close trigger), cleaning 1300+ duplicate/orphan graph nodes accumulated before unique constraints existed, and resolving two latent boundary bugs (WhatsApp JSON parse, missing device_tokens table).

## Issues Fixed

### 1. `close_task_edges()` Trigger Crash
- **Root cause**: Subquery `SELECT id FROM graph_nodes WHERE db_record_id = NEW.id::text AND type = 'task'` returned 19 rows for task 228 (1 current + 18 archived duplicates with same `db_record_id`), causing "more than one row returned by a subquery".
- **Fix**: Added `AND is_current = true` to both subqueries in the trigger function.
- **Verification**: Both task 228 and 252 closed successfully without crash after fix.

### 2. Graph Node Duplicates & Orphans
- **Root cause**: Before `unique_graph_nodes_normalized_label_type` index was added (Jul 9), `ON CONFLICT (normalized_label, type)` was a no-op — every `write_graph_edges_for_task()` or `insert_extracted_entities()` call created a new row. Backfill also processed archived task versions.
- **Scale**: 1235 task nodes → 170 unique, 855 memory nodes → 848 unique.
- **Cleanup** (migration `40_cleanup_duplicate_graph_nodes.sql`):
  - Fixed trigger with `is_current = true`
  - Set `db_record_id` on 104 orphan task nodes (had `metadata->>task_id` but no `db_record_id`)
  - Set `db_record_id` on 328 orphan memory nodes
  - Deleted 26 edgeless backfill orphan nodes (no task_id, no edges, no FK refs)
  - Did NOT delete 1065 archived task nodes — they form version chains via `supersedes_id` FK

### 3. WhatsApp JSON Parse Crash
- **Root cause**: `json.loads(response.text)` at `whatsapp_ingest.py:78` with no error handling — intermittent malformed Gemini output crashed message processing.
- **Fix**: Wrapped in try/except, returns safe `fyi` fallback dict.

### 4. Push Notification Missing Table
- **Root cause**: `send_push_notification()` at `push_notification.py:80` queried `device_tokens` table that was never created. Flutter client was already registered and calling `/api/register-device`, but the table didn't exist.
- **Fix**: Created `device_tokens` table (migration `41_create_device_tokens.sql`) + added try/except safety net.

## DB Migrations
- `40_cleanup_duplicate_graph_nodes.sql` — trigger fix + node cleanup
- `41_create_device_tokens.sql` — push notification table

## Test Data Cleanup
Deleted 3 test tasks (1738 "Prepare digital marketing quote", 1612 "Buy groceries", 1609 "Schedule Qhord review") + their 6 associated graph nodes + Google Calendar event for 1609.

## Key Files
- `core/skills/whatsapp_ingest.py` — JSON parse try/except
- `core/services/push_notification.py` — device_tokens try/except
- `db/40_cleanup_duplicate_graph_nodes.sql` — trigger + cleanup migration
- `db/41_create_device_tokens.sql` — device_tokens table
- `product-summary/42-temporal-versioning-expansion.md` — temporal trigger docs
- `product-summary/38-push-notifications.md` — push notification docs
