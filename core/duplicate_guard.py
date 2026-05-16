import re

STOPWORDS = {
    'the', 'and', 'for', 'to', 'from', 'once', 'complete', 'notify',
    'please', 'then', 'also', 'with', 'after', 'when', 'done', 'this',
    'that', 'your', 'have', 'been', 'into', 'over', 'more', 'some',
    'about', 'other', 'while', 'still', 'just', 'each', 'will', 'can',
    'get', 'see', 'use', 'say', 'could', 'would', 'should', 'much',
}

GENERIC_VERBS = {
    'transfer', 'process', 'review', 'setup', 'update', 'submit',
    'prepare', 'confirm', 'schedule', 'forward', 'approve', 'reject',
    'coordinate', 'arrange', 'discuss', 'contact', 'follow', 'send',
    'draft', 'reply', 'share', 'provide', 'collect', 'manage', 'call',
}


def normalize_title(title: str) -> str:
    normalized = title.lower()
    normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def extract_core(title: str) -> set:
    normalized = re.sub(r'[₹,\-]', ' ', title.lower())
    normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
    words = [w for w in normalized.split() if len(w) > 3 and w not in STOPWORDS]
    return set(words)


def _extract_discriminators(title: str) -> set:
    normalized = normalize_title(title)
    return set(re.findall(r'\bq[1-4]\b|\b\d{4}\b|\b\d{3,}\b', normalized))


def check_duplicate(new_title: str, task_list: list) -> dict:
    """Check if new_title is a near-duplicate of any existing task.

    Args:
        new_title: The suggested task title to check.
        task_list: List of dicts with keys 'id' and 'title' for active tasks.

    Returns:
        dict with keys:
            result: 'block', 'flag', or 'clear'
            matched_id: int or None
            matched_title: str or None
            is_superset: bool (True if existing core is fully contained in new core)
            ratio: float (overlap / shorter length)
    """
    normalized_new = normalize_title(new_title)
    if not normalized_new:
        return {"result": "clear", "matched_id": None, "matched_title": None,
                "is_superset": False, "ratio": 0.0}

    new_core = extract_core(new_title)
    if len(new_core) < 2:
        return {"result": "clear", "matched_id": None, "matched_title": None,
                "is_superset": False, "ratio": 0.0}

    best = {"result": "clear", "matched_id": None, "matched_title": None,
            "is_superset": False, "ratio": 0.0}

    for task in task_list:
        existing_title = task.get('title', '')
        existing_id = task.get('id')

        # Fast path: exact normalized match
        if normalize_title(existing_title) == normalized_new:
            return {"result": "block", "matched_id": existing_id,
                    "matched_title": existing_title, "is_superset": True, "ratio": 1.0}

        existing_core = extract_core(existing_title)
        if len(existing_core) < 2:
            continue

        # Discriminator check: different quarters/amounts/codes → not a duplicate
        new_disc = _extract_discriminators(new_title)
        ex_disc = _extract_discriminators(existing_title)
        if new_disc and ex_disc and new_disc != ex_disc:
            continue

        overlap = new_core & existing_core
        content_overlap = overlap - GENERIC_VERBS
        shorter = min(len(new_core), len(existing_core))
        ratio = len(overlap) / shorter if shorter > 0 else 0.0

        is_superset = existing_core.issubset(new_core) and len(existing_core) >= 3

        if ratio >= 0.80 and len(content_overlap) >= 1:
            if ratio > best["ratio"]:
                best = {"result": "block", "matched_id": existing_id,
                        "matched_title": existing_title,
                        "is_superset": is_superset, "ratio": ratio}
        elif 0.50 <= ratio < 0.80 and len(content_overlap) >= 1:
            if ratio > best["ratio"]:
                best = {"result": "flag", "matched_id": existing_id,
                        "matched_title": existing_title,
                        "is_superset": is_superset, "ratio": ratio}

    return best
