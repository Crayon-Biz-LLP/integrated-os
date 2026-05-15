import re

BLOCKLIST_PEOPLE = {
    "wife", "parents", "sister's family", "sisters family", "customer",
    "employee", "finance manager", "kids", "author", "narrator",
    "user", "mother", "aunt", "uncle",
}

PEOPLE_TITLES = [
    "pastor ", "dr. ", "dr ", "mr. ", "mr ", "mrs. ", "mrs ",
    "ms. ", "ms ", "rev. ", "rev ", "fr. ", "fr ", "saint ",
]


def normalize_person_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"\(.*?\)", "", name).strip()
    for title in PEOPLE_TITLES:
        if name.startswith(title):
            name = name[len(title):]
            break
    return name.strip()


def is_blocklisted_person(name: str) -> bool:
    return bool(name) and normalize_person_name(name) in BLOCKLIST_PEOPLE
