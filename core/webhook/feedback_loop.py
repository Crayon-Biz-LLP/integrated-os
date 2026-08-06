"""
Classifier Feedback Loop (C1)

Reads corrected observations from subsystem_telemetry and populates
classifier_corrections table. Corrections are injected into the
classify_intent prompt as LEARNED CORRECTIONS.

This was previously reading from audit_logs (FEEDBACK_OVERRIDE format),
but all actual corrections go through emit_observation() → subsystem_telemetry.
Switched source to subsystem_telemetry WHERE outcome='corrected'.

Fail-open: if query fails or returns garbage, skip entirely.
Max 50 rules. Oldest-first eviction when full.
"""
import json
from datetime import datetime, timezone, timedelta

from core.services.db import tenant_aware_client
from core.lib.audit_logger import audit_log_sync

# M3: tenant-aware facade (see core/webhook/utils.py webhook_tenant_scope)
supabase = tenant_aware_client()

MAX_CORRECTIONS = 50

# Filler words excluded from text pattern extraction
_FILLER = {
    'the', 'this', 'that', 'with', 'from', 'have', 'been', 'will',
    'were', 'they', 'their', 'about', 'would', 'could', 'should',
    'just', 'also', 'into', 'your', 'what', 'when', 'then', 'than',
    'true', 'false', 'none', 'null',
}


def _parse_json_field(value) -> str | None:
    """Parse a JSON-stored field back to a plain string.

    subsystem_telemetry stores predicted/actual via json.dumps(),
    so a simple string like 'TASK' is stored as '\"TASK\"'.
    This handles both cases: JSON-encoded strings and raw values.
    """
    if value is None:
        return None
    if isinstance(value, str):
        # Strip JSON quotes if present
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
            return str(parsed)
        except (json.JSONDecodeError, TypeError):
            return value
    return str(value)


def _extract_pattern(features: dict, predicted: str, actual: str) -> str:
    """Build a text pattern from the features dict plus predicted/actual values.

    Takes meaningful feature values (strings >2 chars, not filler) as keywords.
    Falls back to predicted→actual if no useful feature values found.
    """
    # Collect meaningful feature values
    keywords = []
    if isinstance(features, dict):
        for k, v in features.items():
            if isinstance(v, str) and len(v) > 2 and v.lower() not in _FILLER:
                keywords.append(v.lower())
            elif isinstance(v, bool):
                pass  # Skip booleans — too generic
            elif isinstance(v, (int, float)):
                pass  # Skip numbers — not useful as text pattern

    # Take first 3 meaningful keywords
    pattern = ' '.join(keywords[:3]) if keywords else ''

    # Fallback: use predicted→actual as the pattern
    if not pattern or len(pattern) < 5:
        pattern = f"{predicted}→{actual}" if predicted and actual else (predicted or actual or '')

    return pattern.lower().strip()


def ingest_feedback_overrides() -> int:
    """Read corrected observations from subsystem_telemetry and upsert into classifier_corrections.

    Reads WHERE outcome='corrected', extracts predicted→actual as old→new intent
    mapping, and upserts into classifier_corrections so the classify_intent prompt
    can inject LEARNED CORRECTIONS.

    Returns the number of new/updated corrections.
    """
    if not supabase:
        return 0

    try:
        # Fetch recent corrections (last 7 days) from subsystem_telemetry
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        res = supabase.table('subsystem_telemetry') \
            .select('subsystem, event_type, features, predicted, actual, outcome') \
            .eq('outcome', 'corrected') \
            .gte('created_at', cutoff) \
            .order('created_at', desc=True) \
            .limit(100) \
            .execute()

        if not res.data:
            return 0

        corrections = []
        for row in res.data:
            predicted = _parse_json_field(row.get('predicted'))
            actual = _parse_json_field(row.get('actual'))
            features = row.get('features', {})

            if not predicted or not actual or predicted == actual:
                continue

            text_pattern = _extract_pattern(features, predicted, actual)
            if not text_pattern or len(text_pattern) < 3:
                continue

            corrections.append({
                'text_pattern': text_pattern,
                'old_intent': str(predicted).upper(),
                'new_intent': str(actual).upper(),
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
