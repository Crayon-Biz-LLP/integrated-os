# ROLE_UPDATE Intent

**Date**: Jul 1, 2026 | **Commits**: ~5

## Problem

Messages like "Marcus Durai is the Pastor of Ashraya Chennai Central" were being classified as TASK or NOTE — creating tasks or notes when the user was simply stating a person's role. No mechanism existed to track people's roles in the `people` table.

## Solution

Added a new `ROLE_UPDATE` classification intent that detects role attribution patterns and updates the `people` table directly — no task or note created.

### Classification

New `ROLE_UPDATE` intent added to the classify prompt with:
- `person_name`, `role_title`, `org_name` JSON fields
- Detection rules for role attribution patterns
- Pronoun resolution via conversation history for "He is the..." patterns

### Handler

`handle_role_update()` in `dispatch.py`:
1. Resolves person via `people` table (ILIKE match)
2. Falls back to `graph_nodes` if not found
3. Creates new `people` entry if person doesn't exist
4. Updates `role` and `organization_name` on the people record
5. Creates `SERVES_AT` graph edge when org exists
6. Sends Telegram confirmation: "✅ Recorded: Marcus Durai → Pastor of Ashraya Chennai Central"

### Guard

Added "pastor" to `BLOCKLIST_PEOPLE` to prevent entity extraction from creating a person node from the role title itself. Role-title duplicates (e.g., pe6847 "Pastor → LEADS → ACC") are automatically rejected.

## Key Files

| File | Purpose |
|------|---------|
| `core/prompts/classify.py` | ROLE_UPDATE intent + detection rules |
| `core/webhook/dispatch.py` | handle_role_update() |
| `core/lib/people_utils.py` | BLOCKLIST_PEOPLE |
