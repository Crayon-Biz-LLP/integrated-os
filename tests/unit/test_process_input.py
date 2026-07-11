"""Tests for ProcessInput dataclass and normalize_and_validate.

Pure unit tests — no database required.
"""
import pytest
from core.lib.process_input import ProcessInput, normalize_and_validate, InvalidInput


class TestNormalizeAndValidate:
    """V1 — normalise_and_validate contract enforcement."""

    # ── Happy path ───────────────────────────────────────────────────────

    def test_valid_task(self):
        """V1.1 — Valid TASK normalises fields correctly."""
        input = ProcessInput(category="TASK", text="Review proposal", source="test",
                             title="Review Equisoft Proposal", priority="Urgent",
                             duration_mins=30, direction="Outbound")

        result = normalize_and_validate(input)

        assert result.category == "TASK"
        assert result.title == "Review Equisoft Proposal"
        assert result.priority == "urgent"
        assert result.duration_mins == 30
        assert result.direction == "outbound"
        assert result.source == "test"
        assert result.memory_type == "note"  # default, not NOTE-specific
        assert result.url is None
        assert result.expires_at is None

    def test_valid_note(self):
        """V1.2 — Valid NOTE strips TASK-only fields, keeps memory_type."""
        input = ProcessInput(category="NOTE", text="Good idea for later", source="test",
                             title="Should be stripped", memory_type="idea",
                             reminder_at="2026-07-15T10:00:00+05:30")

        result = normalize_and_validate(input)

        assert result.category == "NOTE"
        assert result.text == "Good idea for later"
        assert result.memory_type == "idea"
        assert result.title is None
        assert result.reminder_at is None
        assert result.priority == "important"  # default
        assert result.url is None

    def test_valid_resource(self):
        """V1.3 — Valid RESOURCE from explicit url."""
        input = ProcessInput(category="RESOURCE", text="", source="test",
                             url="https://example.com/doc")

        result = normalize_and_validate(input)

        assert result.category == "RESOURCE"
        assert result.url == "https://example.com/doc"
        assert result.title is None

    def test_resource_url_from_text(self):
        """V1.4 — RESOURCE extracts URL from text when url field empty."""
        input = ProcessInput(category="RESOURCE",
                             text="Check this out https://example.com/doc",
                             source="test")

        result = normalize_and_validate(input)

        assert result.url == "https://example.com/doc"

    def test_task_title_falls_back_to_text(self):
        """V1.5 — TASK without explicit title uses text as title."""
        input = ProcessInput(category="TASK", text="Call Marcus about Equisoft",
                             source="test")

        result = normalize_and_validate(input)

        assert result.title == "Call Marcus about Equisoft"

    # ── Edge cases ──────────────────────────────────────────────────────

    def test_task_empty_title_and_text_raises(self):
        """V1.6 — TASK with empty title and empty text raises."""
        input = ProcessInput(category="TASK", text="", source="test", title="")

        with pytest.raises(InvalidInput, match="TASK requires"):
            normalize_and_validate(input)

    def test_task_blank_title_and_text_raises(self):
        """V1.7 — TASK with whitespace-only title and text raises."""
        input = ProcessInput(category="TASK", text="   ", source="test", title="  ")

        with pytest.raises(InvalidInput, match="TASK requires"):
            normalize_and_validate(input)

    def test_resource_without_url_raises(self):
        """V1.8 — RESOURCE with no URL in url or text raises."""
        input = ProcessInput(category="RESOURCE", text="Just some text", source="test")

        with pytest.raises(InvalidInput, match="RESOURCE requires a URL"):
            normalize_and_validate(input)

    def test_unknown_category_raises(self):
        """V1.9 — Unknown category raises."""
        input = ProcessInput(category="UNKNOWN", text="hi", source="test")

        with pytest.raises(InvalidInput, match="Unknown category"):
            normalize_and_validate(input)

    def test_invalid_priority_defaults(self):
        """V1.10 — Non-enum priority defaults to "important"."""
        input = ProcessInput(category="TASK", text="Do the thing", source="test",
                             priority="super-urgent!!!", title="Do the thing")

        result = normalize_and_validate(input)

        assert result.priority == "important"

    def test_valid_priority_preserved(self):
        """V1.11 — Valid enums pass through: urgent, important, normal, low."""
        for prio in ("urgent", "important", "normal", "low"):
            input = ProcessInput(category="TASK", text="Task", source="test",
                                 priority=prio, title="Task")
            result = normalize_and_validate(input)
            assert result.priority == prio, f"Failed for priority={prio}"

    def test_priority_case_normalized(self):
        """V1.12 — Priority casing normalised: "IMPORTANT" → "important"."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             priority="IMPORTANT", title="Task")

        result = normalize_and_validate(input)

        assert result.priority == "important"

    def test_duration_mins_clamped(self):
        """V1.13 — duration_mins clamped to minimum of 1."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", duration_mins=0)

        result = normalize_and_validate(input)

        assert result.duration_mins == 1

        input2 = ProcessInput(category="TASK", text="Task", source="test",
                              title="Task", duration_mins=-5)
        result2 = normalize_and_validate(input2)

        assert result2.duration_mins == 1

    def test_duration_mins_default(self):
        """V1.14 — duration_mins defaults to 15."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", duration_mins=None)

        result = normalize_and_validate(input)

        assert result.duration_mins == 15

    # ── Reminder_at ─────────────────────────────────────────────────────

    def test_naive_reminder_at_normalised(self):
        """V1.15 — Naive reminder_at (no tz) is accepted and normalized with +05:30."""
        input = ProcessInput(category="TASK", text="Meeting", source="test",
                             title="Meeting", reminder_at="2026-07-15T14:00:00")

        result = normalize_and_validate(input)

        assert result.reminder_at is not None
        assert "+05:30" in result.reminder_at or result.reminder_at.endswith("Z")

    def test_tz_aware_reminder_at_preserved(self):
        """V1.16 — Timezone-aware reminder_at passes through."""
        input = ProcessInput(category="TASK", text="Meeting", source="test",
                             title="Meeting", reminder_at="2026-07-15T14:00:00+05:30")

        result = normalize_and_validate(input)

        assert result.reminder_at == "2026-07-15T14:00:00+05:30"

    def test_empty_reminder_at_stays_none(self):
        """V1.17 — Empty reminder_at stays None for TASK."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", reminder_at=None)

        result = normalize_and_validate(input)

        assert result.reminder_at is None

    # ── Idempotency key ──────────────────────────────────────────────────

    def test_idempotency_key_auto_generated(self):
        """V1.18 — idempotency_key auto-generated when not provided."""
        input = ProcessInput(category="TASK", text="Unique task", source="test",
                             title="Unique task")

        result = normalize_and_validate(input)

        assert result.idempotency_key is not None
        assert result.idempotency_key.startswith("auto:")

    def test_idempotency_key_preserved(self):
        """V1.19 — Provided idempotency_key preserved."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", idempotency_key="my-custom-key")

        result = normalize_and_validate(input)

        assert result.idempotency_key == "my-custom-key"

    # ── Category casing ──────────────────────────────────────────────────

    def test_category_lowercase_normalised(self):
        """V1.20 — Lowercase category normalised to uppercase."""
        input = ProcessInput(category="note", text="A note", source="test")

        result = normalize_and_validate(input)

        assert result.category == "NOTE"

    # ── Source ───────────────────────────────────────────────────────────

    def test_source_lowercased_and_stripped(self):
        """V1.21 — Source lowercased and stripped."""
        input = ProcessInput(category="NOTE", text="Note", source="  TEST-SOURCE  ")

        result = normalize_and_validate(input)

        assert result.source == "test-source"

    def test_source_default(self):
        """V1.22 — Empty source defaults to "unknown"."""
        input = ProcessInput(category="NOTE", text="Note", source="")

        result = normalize_and_validate(input)

        assert result.source == "unknown"

    # ── Text trimming ────────────────────────────────────────────────────

    def test_text_stripped(self):
        """V1.23 — Whitespace stripped from text."""
        input = ProcessInput(category="NOTE", text="  My note  ", source="test")

        result = normalize_and_validate(input)

        assert result.text == "My note"

    def test_task_project_name_stripped(self):
        """V1.24 — project_name stripped for TASK."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", project_name="  Equisoft  ")

        result = normalize_and_validate(input)

        assert result.project_name == "Equisoft"

    def test_task_direction_normalised(self):
        """V1.25 — direction lowered and stripped."""
        input = ProcessInput(category="TASK", text="Task", source="test",
                             title="Task", direction="  OUTBOUND  ")

        result = normalize_and_validate(input)

        assert result.direction == "outbound"


class TestProcessInputDataclass:
    """V2 — ProcessInput dataclass field isolation."""

    def test_note_fields_default(self):
        """V2.1 — NOTE defaults: memory_type="note", expires_at=None."""
        input = ProcessInput(category="NOTE", text="hi", source="test")
        assert input.memory_type == "note"
        assert input.expires_at is None

    def test_task_fields_default(self):
        """V2.2 — TASK defaults: priority="important", duration_mins=15."""
        input = ProcessInput(category="TASK", text="hi", source="test")
        assert input.priority == "important"
        assert input.duration_mins == 15
        assert input.direction == "inbound"

    def test_resource_fields_default(self):
        """V2.3 — RESOURCE defaults: url=None."""
        input = ProcessInput(category="RESOURCE", text="hi", source="test")
        assert input.url is None
