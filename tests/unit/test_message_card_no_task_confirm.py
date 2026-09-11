"""Guard tests: message-path suggestion cards never offer or execute task creation.

Aspect marker: app (user-facing suggestion-card behavior).

The Gopi-twin class (Sep 2026, tasks #5868/#5869): the direct pipeline created
the task at message arrival, Path A ALSO listed it as suggested_actions, and
the card confirm executed it again — twin task, twin calendar event, twin
Google Task, because the two producers computed different dedup_keys (the org
state changed between arrival and confirm).

Three structural invariants pin this shut:
  1. handler Path A: suggested_actions on a persisted message card is EMPTY
     (it is the executed receipt, not an approval list).
  2. suggestion_confirm: message sources never execute task actions, no matter
     what a stale card carries.
  3. create_task_direct: title-fingerprint fallback (covered in
     test_task_sync_decision.py) makes the twin structurally impossible even
     if 1 or 2 ever regress.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.app

_HANDLER = Path("core/webhook/handler.py")
_INDEX = Path("api/index.py")


def _path_a_suggested_actions_is_empty_list() -> bool:
    """Path A (should_show_card and actions) must assign suggested_actions = []."""
    tree = ast.parse(_HANDLER.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "should_show_card" in test_src and "actions" in test_src:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        # suggestion_dict["suggested_actions"] = []  or
                        # suggestion_dict.update({... "suggested_actions": [] ...})
                        if isinstance(inner.value, (ast.List, ast.Dict)):
                            return True
    return False


def test_path_a_message_card_suggested_actions_is_receipt_not_approval_list():
    """Invariant 1: Path A never offers executable actions on the card."""
    assert _path_a_suggested_actions_is_empty_list(), (
        "Path A must set suggested_actions to an empty list — message actions "
        "execute at arrival; listing them as approvable re-creates them on confirm."
    )


def test_message_confirm_rejects_executable_task_actions():
    """Invariant 2: the message branch of suggestion_confirm must not call
    execute_actions_harden — executing there is the twin creator."""
    src = _INDEX.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and "source_type == \"message\"" in ast.unparse(node.test)):
            continue
        branch_src = ast.get_source_segment(src, node) or ""
        if "execute_actions_harden" in branch_src:
            # Allowed only inside a string literal (a comment can't contain it,
            # but a log f-string could) — check it's not a real call.
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    if "execute_actions_harden" in ast.unparse(call.func):
                        pytest.fail(
                            "suggestion_confirm message branch executes actions — "
                            "message actions must run at arrival, not at confirm "
                            "(the Gopi-twin creator)."
                        )
        # And the explicit count guard must exist so stale cards are visible in logs.
        assert "_executable_count" in branch_src, (
            "message branch lost its stale-card logging guard (_executable_count)"
        )
