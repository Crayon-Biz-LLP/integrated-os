"""
Backfill Thread Entity Labels (H2)

Populates entity_label on existing conversation_threads rows that have
entity_type and entity_id set but entity_label is NULL.

For organizations: queries organizations.name
For projects: queries projects.name
For people: queries graph_nodes.label

Usage:
    LIVE_DB=true python scripts/backfill_thread_entity_labels.py

Idempotent — safe to re-run. Skips threads that already have entity_label.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.services.db import get_supabase


def backfill_entity_labels():
    supabase = get_supabase()

    # Find entity threads with null entity_label
    res = supabase.table('conversation_threads') \
        .select('id, entity_type, entity_id') \
        .not_.is_('entity_id', 'null') \
        .is_('entity_label', 'null') \
        .neq('thread_type', 'general') \
        .limit(200) \
        .execute()

    rows = res.data or []
    if not rows:
        print("No threads need backfill.")
        return

    total = len(rows)
    updated = 0
    skipped = 0

    for thread in rows:
        tid = thread['id']
        etype = thread.get('entity_type')
        eid = thread.get('entity_id')

        if not eid or not etype:
            skipped += 1
            continue

        label = None
        try:
            if etype == 'organization':
                r = supabase.table('organizations').select('name').eq('id', eid).limit(1).execute()
                if r.data:
                    label = r.data[0].get('name', '')
            elif etype == 'project':
                r = supabase.table('projects').select('name').eq('id', eid).limit(1).execute()
                if r.data:
                    label = r.data[0].get('name', '')
            elif etype == 'person':
                r = supabase.table('graph_nodes').select('label').eq('id', eid).limit(1).execute()
                if r.data:
                    label = r.data[0].get('label', '')
        except Exception:
            pass

        if label:
            supabase.table('conversation_threads') \
                .update({'entity_label': label}) \
                .eq('id', tid) \
                .execute()
            updated += 1
        else:
            skipped += 1

    print(f"Backfill complete: {updated} updated, {skipped} skipped, {total} total")
    return updated


if __name__ == '__main__':
    result = backfill_entity_labels()
    print(f"Done. Result: {result}")
