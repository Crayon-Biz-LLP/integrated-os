"""S4: Pattern Detection — analyzes completion history and surfaces insights.

Runs weekly (Sunday) via sentinel piggyback. Mines:
- Completion velocity (tasks/day by day-of-week)
- Project completion clustering (which projects get done together)
- Time-of-day productivity patterns
- Delegation success rates
- Priority distribution shifts
"""

from datetime import datetime, timezone, timedelta
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

supabase = get_supabase()


def detect_completion_patterns() -> dict:
    """Analyze completion history over the last 30 days.
    Returns a dict with pattern insights for the briefing and serendipity engine.
    """
    now = datetime.now(timezone.utc)
    thirty_days_ago = (now - timedelta(days=30)).isoformat()

    result = {
        'velocity_by_day': {},
        'project_clusters': [],
        'time_of_day': {},
        'delegation_stats': {},
        'priority_shifts': {},
        'insights': [],
    }

    try:
        # 1. Completion velocity by day-of-week
        completed_res = supabase.table('tasks') \
            .select('id, title, completed_at, project_id, priority, organization_id, direction, committed_to') \
            .eq('is_current', True) \
            .eq('status', 'done') \
            .gte('completed_at', thirty_days_ago) \
            .execute()

        completions = completed_res.data or []
        if not completions:
            return result

        day_counts = {}
        hour_counts = {}
        project_completions = {}
        delegation_completed = 0
        delegation_total = 0
        priority_counts = {}

        for c in completions:
            # Day-of-week velocity
            ca = c.get('completed_at', '')
            if ca:
                try:
                    dt = datetime.fromisoformat(str(ca).replace('Z', '+00:00'))
                    day_name = dt.strftime('%a')
                    day_counts[day_name] = day_counts.get(day_name, 0) + 1
                    hour = dt.hour
                    time_bucket = 'morning' if hour < 12 else 'afternoon' if hour < 17 else 'evening'
                    hour_counts[time_bucket] = hour_counts.get(time_bucket, 0) + 1
                except Exception:
                    pass

            # Project clustering
            pid = c.get('project_id')
            if pid:
                if pid not in project_completions:
                    project_completions[pid] = 0
                project_completions[pid] += 1

            # Priority distribution
            pri = c.get('priority', 'important')
            priority_counts[pri] = priority_counts.get(pri, 0) + 1

        result['velocity_by_day'] = day_counts
        result['time_of_day'] = hour_counts
        result['priority_shifts'] = priority_counts

        # 2. Project completion clusters (projects with 3+ completions)
        for pid, count in project_completions.items():
            if count >= 3:
                proj_res = supabase.table('projects').select('name').eq('id', pid).maybe_single().execute()
                if proj_res and proj_res.data:
                    result['project_clusters'].append({
                        'project': proj_res.data['name'],
                        'completions': count,
                    })

        # 3. Delegation success rate
        waiting_tasks = supabase.table('tasks') \
            .select('id, direction, committed_to') \
            .eq('is_current', True) \
            .eq('direction', 'waiting_on') \
            .execute()
        delegation_total = len(waiting_tasks.data or [])
        delegation_completed = sum(1 for t in (waiting_tasks.data or []) if t.get('status') == 'done')
        result['delegation_stats'] = {
            'total': delegation_total,
            'completed': delegation_completed,
            'rate': delegation_completed / max(delegation_total, 1),
        }

        # 4. Generate insights
        if day_counts:
            best_day = max(day_counts, key=day_counts.get)
            worst_day = min(day_counts, key=day_counts.get)
            if day_counts[best_day] > day_counts.get(worst_day, 0) * 1.5:
                result['insights'].append(
                    f"📊 Peak completion day: {best_day} ({day_counts[best_day]} tasks). "
                    f"Slowest: {worst_day} ({day_counts.get(worst_day, 0)} tasks)."
                )

        if hour_counts:
            best_time = max(hour_counts, key=hour_counts.get)
            result['insights'].append(f"⏰ Most productive time: {best_time} ({hour_counts[best_time]} completions).")

        if delegation_total > 0 and delegation_completed / max(delegation_total, 1) < 0.5:
            result['insights'].append(
                f"⏳ Delegation bottleneck: {delegation_completed}/{delegation_total} waiting tasks resolved. "
                "Consider following up."
            )

        if result['project_clusters']:
            top_project = max(result['project_clusters'], key=lambda x: x['completions'])
            result['insights'].append(
                f"🔥 Hot project: {top_project['project']} ({top_project['completions']} completions in 30d)."
            )

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Pattern detection error: {e}")

    return result


def format_patterns_for_briefing(patterns: dict) -> str:
    """Format pattern insights for injection into the briefing prompt."""
    insights = patterns.get('insights', [])
    if not insights:
        return ""

    lines = ["📊 WEEKLY PATTERNS (auto-detected):"]
    lines.extend(insights[:5])
    return "\n".join(lines)


def format_patterns_for_serendipity(patterns: dict) -> str:
    """Format pattern data for the serendipity engine to consume."""
    parts = []

    # Project clusters suggest cross-domain connections
    for pc in patterns.get('project_clusters', [])[:3]:
        parts.append(f"active_project:{pc['project']}")

    # Velocity patterns
    velocity = patterns.get('velocity_by_day', {})
    if velocity:
        best_day = max(velocity, key=velocity.get)
        parts.append(f"peak_day:{best_day}")

    return " | ".join(parts) if parts else ""
