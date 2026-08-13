"""Phase 5 provider-shape tests (no network required).

Covers `core/llm/providers.py` response_schema plumbing:
- `openrouter_response_format` maps config → json_schema / json_object / None
- `_schema_rejection` detects schema-rejection errors (for graceful degradation)
- `PLANNER_ACTIONS_SCHEMA` is shape-level (operation enum + object params)

Run: python -m pytest tests/unit/test_providers_shape.py -v
"""

from core.llm.providers import _schema_rejection, openrouter_response_format
from core.prompts.planner import PLANNER_ACTIONS_SCHEMA

SCHEMA = {"type": "object", "properties": {"actions": {"type": "array"}}}


def test_openrouter_json_schema_with_response_schema():
    fmt = openrouter_response_format({"response_mime_type": "application/json", "response_schema": SCHEMA})
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMA
    assert fmt["json_schema"]["strict"] is False
    assert "name" in fmt["json_schema"]


def test_openrouter_json_object_without_schema():
    fmt = openrouter_response_format({"response_mime_type": "application/json"})
    assert fmt == {"type": "json_object"}


def test_openrouter_none_without_json_config():
    assert openrouter_response_format({}) is None
    assert openrouter_response_format(None) is None
    assert openrouter_response_format({"response_mime_type": "text/plain"}) is None


def test_schema_rejection_detection():
    assert _schema_rejection(Exception("Invalid JSON schema at properties.actions")) is True
    assert _schema_rejection(Exception("response_schema is not supported")) is True
    assert _schema_rejection(Exception("Invalid argument: schema must be an object")) is True
    assert _schema_rejection(Exception("429 Resource has been exhausted")) is False
    assert _schema_rejection(Exception("connection reset")) is False


def test_planner_shape_schema_has_operation_enum():
    assert PLANNER_ACTIONS_SCHEMA["type"] == "object"
    assert PLANNER_ACTIONS_SCHEMA["required"] == ["actions"]
    items = PLANNER_ACTIONS_SCHEMA["properties"]["actions"]["items"]
    ops = items["properties"]["operation"]["enum"]
    for op in (
        "create_task", "create_note", "create_event", "query_info", "close_task",
        "cancel_recurring", "suppress_instance", "modify_recurring", "reschedule",
        "update_metadata", "delete_event", "no_op",
    ):
        assert op in ops
    assert items["required"] == ["operation"]
    # Shape-level: params is unconstrained object — per-op required fields stay
    # in the Phase 1 typed models (strict backstop).
    assert items["properties"]["params"] == {"type": "object"}
