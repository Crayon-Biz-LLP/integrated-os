"""
Extract pulse/engine.py into sub-modules.
Usage: python scripts/_extract_pulse.py
"""
from pathlib import Path

ENGINE = Path("core/pulse/engine.py")
OUT = Path("core/pulse")

# (name, start, end, target_file)
EXTRACTIONS = [
    # llm.py
    ("is_already_in_email_queue", 121, 154, "llm.py"),
    ("call_gemini_with_retry", 392, 424, "llm.py"),
    ("SimpleResponse", 427, 430, "llm.py"),
    ("_jitter", 433, 435, "llm.py"),
    ("parse_json_response", 438, 462, "llm.py"),
    ("call_llm_with_fallback", 465, 603, "llm.py"),
    ("_call_openrouter", 606, 641, "llm.py"),
    ("get_embedding", 644, 659, "llm.py"),
    ("cosine_similarity", 662, 671, "llm.py"),
    ("OPENROUTER_API_KEY", 103, 103, "llm.py"),
    ("OPENROUTER_BASE_URL", 104, 104, "llm.py"),
    ("PULSE_ENABLE_OPENROUTER_FALLBACK", 105, 105, "llm.py"),
    ("PULSE_HTTP_REFERER", 106, 106, "llm.py"),
    ("PULSE_APP_NAME", 107, 107, "llm.py"),
    ("GEMMA_FALLBACK_MODEL", 109, 109, "llm.py"),
    ("GEMMA_SPEED_MODEL", 110, 110, "llm.py"),
    ("OPENROUTER_MODEL", 111, 111, "llm.py"),
    ("RETRYABLE_ERRORS", 113, 113, "llm.py"),
    ("NON_RETRYABLE_ERRORS", 114, 114, "llm.py"),
    ("gemini_client", 156, 156, "llm.py"),
    ("EMBEDDING_MODEL", 158, 158, "llm.py"),
    ("EMBEDDING_DIMENSION", 159, 159, "llm.py"),
    ("BRIEFING_MODEL", 161, 161, "llm.py"),

    # utils.py
    ("format_error", 40, 43, "utils.py"),
    ("get_project_name", 164, 168, "utils.py"),
    ("build_routing_context", 171, 207, "utils.py"),
    ("normalize_mission_title", 383, 390, "utils.py"),

    # graph.py
    ("write_graph_edges_for_task", 210, 305, "graph.py"),
    ("hybrid_search_graph", 741, 802, "graph.py"),
    ("check_task_dependencies", 807, 888, "graph.py"),
    ("analyze_communication_patterns", 893, 961, "graph.py"),
    ("fetch_hybrid_graph_context", 1799, 1828, "graph.py"),
    ("fetch_graph_task_context", 1831, 1926, "graph.py"),

    # memory.py
    ("write_outcome_memory", 308, 329, "memory.py"),
    ("get_recent_memories_for_briefing", 674, 738, "memory.py"),
    ("retrieve_hindsight_memories", 1153, 1219, "memory.py"),
    ("generate_after_action_report", 1222, 1262, "memory.py"),
    ("detect_temporal_patterns", 966, 1009, "memory.py"),
    ("serendipity_engine", 1014, 1075, "memory.py"),
    ("adaptive_briefing_learner", 1080, 1150, "memory.py"),

    # practices.py
    ("detect_practices", 2121, 2626, "practices.py"),
    ("build_practice_edges", 2629, 2761, "practices.py"),
    ("build_practice_correlations", 2764, 2861, "practices.py"),
    ("sync_practice_canonical_pages", 2864, 3001, "practices.py"),
    ("build_rhythms_section", 3004, 3159, "practices.py"),

    # calendar.py
    ("get_calendar_context", 1677, 1691, "calendar.py"),
    ("check_conflict", 1694, 1714, "calendar.py"),
    ("sync_to_calendar", 1716, 1748, "calendar.py"),
    ("sync_completed_tasks_from_google", 1413, 1489, "calendar.py"),
    ("MemoryCache", 1265, 1272, "calendar.py"),
    ("get_google_calendar_events", 1613, 1642, "calendar.py"),
    ("get_google_calendar_events_range", 1645, 1674, "calendar.py"),

    # pipeline.py
    ("update_heartbeat", 1929, 1938, "pipeline.py"),
    ("check_pipeline_health", 1940, 2017, "pipeline.py"),
    ("retry_failed_operations", 2035, 2118, "pipeline.py"),

    # resources.py
    ("fetch_url_metadata", 1275, 1292, "resources.py"),
    ("batch_enrich_resources", 1295, 1408, "resources.py"),
]

def read_lines(filepath, start, end):
    with open(filepath) as f:
        lines = f.readlines()
    return lines[start - 1:end]

def main():
    files = {}
    for (name, s, e, target) in EXTRACTIONS:
        files.setdefault(target, []).append((name, s, e))

    for target, items in sorted(files.items()):
        lines = []
        lines.append(f'# core/pulse/{target} — extracted from engine.py\n')
        lines.append('\n')
        for name, s, e in items:
            chunk = read_lines(ENGINE, s, e)
            chunk = [line.rstrip() + '\n' for line in chunk]
            lines.extend(chunk)
            lines.append('\n')

        # Remove trailing blank lines
        while lines and lines[-1].strip() == '':
            lines.pop()

        outpath = OUT / target
        with open(outpath, 'w') as f:
            f.write(''.join(lines))
        print(f"  Created {outpath} ({len(items)} items, {len(''.join(lines).splitlines())} lines)")

if __name__ == '__main__':
    main()
