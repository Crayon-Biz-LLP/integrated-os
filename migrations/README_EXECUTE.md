# SQL Migrations - Execution Instructions

## How to Run These Migrations

1. Go to **Supabase Dashboard** → Your Project → **SQL Editor**
2. Copy-paste each migration file's content
3. Click **Run** (or Ctrl+Enter)
4. Check for errors in the output

## Migration Order

### 1. Migration 009: Add ON DELETE CASCADE (PREVENTS ORPHANS)
**File:** `migrations/009_add_cascade_delete.sql`

**What it does:**
- Adds `ON DELETE CASCADE` to foreign keys:
  - `tasks.project_id` → `projects.id`
  - `memories.project_id` → `projects.id`
  - `graph_edges.source_node_id` → `graph_nodes.id`
  - `graph_edges.target_node_id` → `graph_nodes.id`

**Why:** When a project/node is deleted, related records are automatically deleted (no orphans).

**Verification after running:**
```sql
-- Check if CASCADE is applied
SELECT conname, confupdtype, confdeltype 
FROM pg_constraint 
WHERE conrelid IN ('tasks'::regclass, 'memories'::regclass, 'graph_edges'::regclass)
AND contype = 'f';

-- confdeltype = 'c' means CASCADE
```

---

### 2. Migration 010: Fix 7-day raw_dumps Deletion (Optional)
**File:** `migrations/010_fix_raw_dumps_retention.sql`

**What it does:**
- Changes `raw_dumps` cleanup from 7 days to **30 days**
- Aligns with other cleanup windows (memories: 30 days, edges: 30 days)

**Why:** 7 days is too aggressive - you might need to reprocess dumps within 30 days.

**To create this migration:**
```sql
-- Update cleanup_orphans.py logic (already done)
-- Update pulse_cli.py --cleanup logic (already done)
-- This is just a documentation migration
```

---

## Root Cause Fixes (Already Applied to Code)

### 1. `backfill_graph.py` - Edge Creation Validation
**File:** `core/skills/backfill_graph.py` (lines 541-580)

**Fix:** Added node existence validation before edge insertion:
- Validates `source_id` and `target_id` exist in `graph_nodes` table
- Skips edge creation if nodes don't exist
- Logs warning for orphaned edges

**Why:** Prevents edges pointing to non-existent nodes.

### 2. `pulse/engine.py` - Versioned Updates
**File:** `core/pulse/engine.py`

**Fix:** Refactored critical `.update()` calls to use `versioned_update()`:
- ✅ `resources.mission_id` update (line 1286)
- ✅ `tasks` status/reminder update (line 2798)
- ✅ `tasks` Google ID sync (line 3047)
- ⏳ Remaining: `raw_dumps` status updates (no versioning needed - staging table)

**Why:** Creates temporal lineage for important state changes.

---

## Post-Migration Checklist

1. **Run Migration 009** in Supabase Dashboard
2. **Test CASCADE delete:**
   ```sql
   -- Test: Delete a project, check if tasks/memories are cascade-deleted
   DELETE FROM projects WHERE id = 'some-test-id';
   SELECT COUNT(*) FROM tasks WHERE project_id = 'some-test-id'; -- Should be 0
   ```
3. **Run cleanup_orphans.py** to remove any existing orphans:
   ```bash
   python core/agents/cleanup_orphans.py --dry-run  # Check first
   python core/agents/cleanup_orphans.py            # Actually delete
   ```
4. **Monitor audit_logs** for orphan creation attempts:
   ```sql
   SELECT * FROM audit_logs 
   WHERE message LIKE '%orphan%' OR message LIKE '%missing node%'
   ORDER BY created_at DESC LIMIT 20;
   ```

---

## Files Modified (Root Cause Fixes)

1. ✅ `core/skills/backfill_graph.py` - Edge validation before insert
2. ✅ `core/pulse/engine.py` - Versioned updates for tasks/resources
3. ✅ `core/agents/cleanup_orphans.py` - Added warning for orphaned edges (should be rare now)
4. ✅ `migrations/009_add_cascade_delete.sql` - CASCADE foreign keys (needs manual run)
5. ⏳ `migrations/010_fix_raw_dumps_retention.sql` - 30-day retention (optional)

---

## Next Steps

1. **Run Migration 009** in Supabase Dashboard (5 minutes)
2. **Run agents/cleanup_orphans.py** to remove existing orphans
3. **Monitor** for new orphan creation (should be near zero now)
4. **Optional:** Implement Phase-1 Janitor Daemon + DLQ logic (if needed)
