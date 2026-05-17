"""
Extract webhook/handler.py into sub-modules based on known line ranges.
Usage: python scripts/_extract_webhook.py
"""
import ast, sys
from pathlib import Path

HANDLER = Path("core/webhook/handler.py")
OUT = Path("core/webhook")

# Line ranges from analysis: (name, (start, end), target_file)
EXTRACTIONS = [
    # telegram.py (leaf module — no webhook-internal imports)
    ("_chunk_message", (1443, 1458), "telegram.py"),
    ("send_telegram", (1461, 1526), "telegram.py"),
    ("download_telegram_file", (740, 760), "telegram.py"),
    ("KEYBOARD", (1846, 1855), "telegram.py"),

    # classify.py (leaf module — no webhook-internal imports)
    ("gemini_client", (338, 338), "classify.py"),
    ("EMBEDDING_MODEL", (340, 340), "classify.py"),
    ("CLASSIFICATION_MODEL", (341, 341), "classify.py"),
    ("EMBEDDING_DIMENSION", (342, 342), "classify.py"),
    ("call_gemini_with_retry", (383, 419), "classify.py"),
    ("get_embedding", (422, 435), "classify.py"),
    ("classify_intent", (438, 509), "classify.py"),
    ("OPPORTUNITY_PATTERNS", (186, 198), "classify.py"),
    ("detect_opportunity_language", (201, 206), "classify.py"),
    ("UPDATE_TRIGGER_WORDS", (89, 90), "classify.py"),
    ("check_task_overlap_for_update", (92, 118), "classify.py"),
    ("INTENT_OPTIONS", (1129, 1137), "classify.py"),
    ("INTENT_BY_KEYWORD", (1139, 1142), "classify.py"),

    # utils.py (leaf module — no webhook-internal imports)
    ("MemoryCache", (53, 58), "utils.py"),
    ("get_google_creds", (60, 68), "utils.py"),
    ("is_already_in_tasks_table", (70, 86), "utils.py"),
    ("supabase", (48, 51), "utils.py"),
    ("is_recent_raw_dump", (165, 183), "utils.py"),
    ("get_recent_context", (727, 737), "utils.py"),
    ("trigger_github_pulse", (345, 380), "utils.py"),
    ("hybrid_search_graph", (1082, 1126), "utils.py"),

    # email.py
    ("process_email_pending_decision", (209, 335), "email.py"),
    ("get_gmail_service", (1529, 1537), "email.py"),
    ("send_draft_reply", (1540, 1643), "email.py"),
    ("send_outlook_draft", (1646, 1701), "email.py"),
    ("handle_ed_command", (1704, 1843), "email.py"),

    # multimodal.py
    ("process_multimodal_content", (763, 903), "multimodal.py"),

    # commands.py
    ("handle_practices_command", (2237, 2340), "commands.py"),
    ("handle_status_command", (2343, 2425), "commands.py"),
    ("handle_undo_command", (2499, 2638), "commands.py"),
    ("handle_command", (2641, 2769), "commands.py"),

    # dispatch.py (the heavy one)
    ("_format_task_line", (512, 524), "dispatch.py"),
    ("handle_daily_brief", (527, 724), "dispatch.py"),
    ("handle_confident_task", (906, 963), "dispatch.py"),
    ("handle_confident_note", (966, 1057), "dispatch.py"),
    ("handle_clarification", (1060, 1079), "dispatch.py"),
    ("ask_intent_disambiguation", (1145, 1158), "dispatch.py"),
    ("resolve_disambiguation", (1161, 1173), "dispatch.py"),
    ("ask_task_or_note_confirmation", (1176, 1193), "dispatch.py"),
    ("resolve_task_note_confirmation", (1196, 1209), "dispatch.py"),
    ("route_by_intent", (1212, 1241), "dispatch.py"),
    ("interrogate_brain", (1244, 1436), "dispatch.py"),
    ("handle_noise", (1439, 1440), "dispatch.py"),
    ("ask_task_update_confirmation", (121, 140), "dispatch.py"),
    ("resolve_task_update_confirmation", (143, 162), "dispatch.py"),
    ("handle_declare_practice", (2428, 2496), "dispatch.py"),
]

def read_lines(filepath, start, end):
    """Read inclusive line range from file (1-indexed)."""
    with open(filepath) as f:
        lines = f.readlines()
    return lines[start - 1:end]

def build_file_content(target_file, extract_names):
    """Build content for a sub-module file."""
    lines = []
    lines.append(f'# core/webhook/{target_file} — extracted from handler.py\n')
    lines.append('"""Webhook sub-module."""\n')
    lines.append('')
    for (name, (s, e), _) in EXTRACTIONS:
        if name in extract_names:
            chunk = read_lines(HANDLER, s, e)
            # Remove trailing whitespace
            chunk = [line.rstrip() + '\n' for line in chunk]
            lines.extend(chunk)
            lines.append('\n')
    return ''.join(lines)

def main():
    files = {}
    for (name, rng, target) in EXTRACTIONS:
        files.setdefault(target, []).append(name)

    for target, names in sorted(files.items()):
        content = build_file_content(target, names)
        outpath = OUT / target
        with open(outpath, 'w') as f:
            f.write(content)
        print(f"  Created {outpath} ({len(names)} items, {len(content.splitlines())} lines)")

if __name__ == '__main__':
    main()
