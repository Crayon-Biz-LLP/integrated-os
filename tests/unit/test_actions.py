from core.actions import (
    ActionResult, render_actions, validate_action_claims, 
    begin_action_context, accumulate_action, snapshot_action_context, clear_action_context
)

def test_render_actions_executed():
    res = ActionResult(action_type="task_create", status="executed", entity_id=123, human_label="Buy milk")
    lines = render_actions([res])
    assert "✅ Task created: Buy milk" in lines[0]

def test_render_actions_failed():
    res = ActionResult(action_type="task_create", status="failed", evidence={"error": "db timeout"})
    lines = render_actions([res])
    assert "⚠️ Task create failed: db timeout" in lines[0]

def test_validate_action_claims_unbacked():
    text = "I have added the task to your list."
    evidence = []
    cleaned, downgrades = validate_action_claims(text, evidence)
    assert cleaned == "I can create a task for this to your list."
    assert len(downgrades) == 1
    assert downgrades[0]["action_type"] == "task_create"

def test_validate_action_claims_backed():
    text = "I have added the task."
    evidence = [ActionResult(action_type="task_create", status="executed", entity_id=1)]
    cleaned, downgrades = validate_action_claims(text, evidence)
    assert cleaned == text
    assert len(downgrades) == 0

def test_validate_action_claims_monitoring():
    text = "I will monitor this transfer."
    evidence = []
    cleaned, downgrades = validate_action_claims(text, evidence)
    assert cleaned == "I can set up a reminder to check this this transfer."

def test_contextvar_lifecycle():
    begin_action_context()
    res = ActionResult(action_type="task_create", status="executed", entity_id=1)
    accumulate_action(res)
    snap = snapshot_action_context()
    assert len(snap) == 1
    clear_action_context()
    assert len(snapshot_action_context()) == 0

