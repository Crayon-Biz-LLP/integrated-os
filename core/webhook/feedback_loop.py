"""
Classifier Feedback Loop (C1)

Reads FEEDBACK_OVERRIDE events from audit_logs and populates
classifier_corrections table. Corrections are injected into the
classify_intent prompt as LEARNED CORRECTIONS.

Fail-open: if audit_logs query fails or returns garbage, skip entirely.
Max 50 rules. Oldest-first eviction when full.
"""
import re
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()

MAX_CORRECTIONS = 50

# Pattern to parse FEEDBACK_OVERRIDE log messages
# Format: FEEDBACK_OVERRIDE: user corrected 'OLD' → 'NEW' | text='TEXT'
OVERRIDE_PATTERN = re.compile(
    r"FEEDBACK_OVERRIDE: user corrected '(\w+)' → '(\w+)'\s*\|\s*text='(.{0,80})'"
)


def _extract_pattern(text: str) -> str:
    """Extract a simplified keyword pattern from raw text for matching.

    Takes the first 3 meaningful words (>3 chars, not filler) as the pattern.
    This gives a fuzzy but stable matching key.
    """
    filler = {'the', 'this', 'that', 'with', 'from', 'have', 'been', 'will',
              'were', 'they', 'their', 'about', 'would', 'could', 'should',
              'just', 'also', 'into', 'your', 'what', 'when', 'then', 'than'}
    words = [w.lower().strip('.,!?;:\'"') for w in text.split()
             if len(w) > 3 and w.lower() not in filler]
    # Take first 3 meaningful words
    return ' '.join(words[:3]) if words else text[:30].lower().strip()


def ingest_feedback_overrides() -> int:
    """Read FEEDBACK_OVERRIDE events from audit_logs and upsert into classifier_corrections.

    Returns the number of new/updated corrections.
    """
    if not supabase:
        return 0

    try:
        # Fetch recent FEEDBACK_OVERRIDE events (last 7 days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        res = supabase.table('audit_logs') \
            .select('message') \
            .eq('service', 'webhook') \
            .ilike('message', '%FEEDBACK_OVERRIDE%') \
            .gte('created_at', cutoff) \
            .order('created_at', desc=True) \
            .limit(100) \
            .execute()

        if not res.data:
            return 0

        corrections = []
        for row in res.data:
            msg = row.get('message', '')
            match = OVERRIDE_PATTERN.search(msg)
            if not match:
                continue
            old_intent, new_intent, raw_text = match.groups()
            pattern = _extract_pattern(raw_text)
            if not pattern or len(pattern) < 5:
                continue
            corrections.append({
                'text_pattern': pattern,
                'old_intent': old_intent.upper(),
                'new_intent': new_intent.upper(),
            })

        if not corrections:
            return 0

        # Upsert each correction
        upserted = 0
        for c in corrections:
            try:
                existing = supabase.table('classifier_corrections') \
                    .select('id, count') \
                    .eq('text_pattern', c['text_pattern']) \
                    .eq('old_intent', c['old_intent']) \
                    .eq('new_intent', c['new_intent']) \
                    .maybe_single() \
                    .execute()

                if existing and existing.data:
                    # Increment count and update last_seen
                    supabase.table('classifier_corrections').update({
                        'count': existing.data['count'] + 1,
                        'last_seen': datetime.now(timezone.utc).isoformat(),
                    }).eq('id', existing.data['id']).execute()
                else:
                    # Check max capacity — evict oldest if full
                    count_res = supabase.table('classifier_corrections') \
                        .select('id') \
                        .execute()
                    current_count = len(count_res.data) if count_res.data else 0
                    if current_count >= MAX_CORRECTIONS:
                        # Evict oldest by first_seen
                        oldest = supabase.table('classifier_corrections') \
                            .select('id') \
                            .order('first_seen', asc=True) \
                            .limit(1) \
                            .maybe_single() \
                            .execute()
                        if oldest and oldest.data:
                            supabase.table('classifier_corrections') \
                                .delete() \
                                .eq('id', oldest.data['id']) \
                                .execute()

                    # Insert new correction
                    supabase.table('classifier_corrections').insert({
                        'text_pattern': c['text_pattern'],
                        'old_intent': c['old_intent'],
                        'new_intent': c['new_intent'],
                        'count': 1,
                        'enabled': True,
                        'created_by': 'feedback_loop',
                    }).execute()
                upserted += 1
            except Exception as e:
                audit_log_sync('classifier', 'WARNING', f'Failed to upsert correction {c}: {e}')

        audit_log_sync('classifier', 'INFO', f'Feedback ingestion complete: {upserted} corrections processed')
        return upserted

    except Exception as e:
        audit_log_sync('classifier', 'WARNING', f'Feedback ingestion failed (non-critical): {e}')
        return 0


def get_learned_corrections() -> str:
    """Fetch enabled corrections and format as a prompt section.

    Returns empty string if no corrections exist or on failure (fail-open).
    """
    if not supabase:
        return ''

    try:
        res = supabase.table('classifier_corrections') \
            .select('text_pattern, old_intent, new_intent, count') \
            .eq('enabled', True) \
            .order('count', desc=True) \
            .limit(20) \
            .execute()

        if not res.data:
            return ''

        lines = ['LEARNED CORRECTIONS (from past user overrides):']
        for r in res.data:
            lines.append(
                f'- "{r["text_pattern"]}" → {r["new_intent"]} '
                f'(was {r["old_intent"]}, corrected {r["count"]}x)'
            )
        return '\n'.join(lines)

    except Exception:
        return ''
