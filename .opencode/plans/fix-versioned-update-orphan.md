# Fix: versioned_update orphaning records + data repair

## Bug
`versioned_update` at `core/pulse.py:63` marks the old record `is_current=false` **before** the INSERT of the new version succeeds. If the INSERT fails (network, constraint, etc.), the fallback direct update at line 92 doesn't restore `is_current=true`, leaving the record orphaned: is_current=false, status unchanged, no superseding version.

This caused tasks 133 and 136 to have `is_current=false, status='todo'` — invisible to the AI's active tasks briefing (after the `.eq('is_current', True)` fix at line 2012).

## Fix 1: Reorder versioned_update

Swap the order: INSERT the new version first, then mark the old record as non-current. Also fix the fallback to include `is_current=True`.

**File:** `core/pulse.py` lines 60-93

**Current code (lines 60-93):**
```python
old_record = current.data[0]

# Mark old as not current
supabase.table(table_name).update({"is_current": False}).eq('id', record_id).execute()

# Prepare new version
new_record = {
    **{k: v for k, v in old_record.items() 
       if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id']},
    **update_data,
    'is_current': True
}

# Get next version number
old_version = old_record.get('version',0) or 0
new_record['version'] = old_version + 1
new_record['supersedes_id'] = record_id

# Insert new version
result = supabase.table(table_name).insert(new_record).execute()

# Log the change
if change_source or change_reason:
    audit_log_sync("pulse", "INFO", 
        f"Versioned update: {table_name}:{record_id} v{new_record['version']}", 
        {"source": change_source, "reason": change_reason, "user_id": user_id})

return bool(result.data)

except Exception as e:
    # Fallback to regular update
    audit_log_sync("pulse", "WARNING", f"Versioned update failed for {table_name}:{record_id}, falling back to update: {e}")
    supabase.table(table_name).update(update_data).eq('id', record_id).execute()
    return True
```

**Replace with:**
```python
old_record = current.data[0]

# Prepare new version
new_record = {
    **{k: v for k, v in old_record.items() 
       if k not in ['id', 'created_at', 'version', 'is_current', 'supersedes_id']},
    **update_data,
    'is_current': True
}

# Get next version number
old_version = old_record.get('version',0) or 0
new_record['version'] = old_version + 1
new_record['supersedes_id'] = record_id

# Insert new version FIRST (so failure doesn't orphan the old record)
result = supabase.table(table_name).insert(new_record).execute()

# Mark old as not current (only after new insert succeeds)
supabase.table(table_name).update({"is_current": False}).eq('id', record_id).execute()

# Log the change
if change_source or change_reason:
    audit_log_sync("pulse", "INFO", 
        f"Versioned update: {table_name}:{record_id} v{new_record['version']}", 
        {"source": change_source, "reason": change_reason, "user_id": user_id})

return bool(result.data)

except Exception as e:
    # Fallback to regular update — include is_current=True to avoid orphaned records
    audit_log_sync("pulse", "WARNING", f"Versioned update failed for {table_name}:{record_id}, falling back to update: {e}")
    fallback_data = {**update_data, 'is_current': True}
    supabase.table(table_name).update(fallback_data).eq('id', record_id).execute()
    return True
```

## Fix 2: Data repair SQL

Run in Supabase SQL Editor:
```sql
UPDATE tasks SET is_current = true 
WHERE id IN (133, 136) 
  AND version = 1 
  AND supersedes_id IS NULL;
```
