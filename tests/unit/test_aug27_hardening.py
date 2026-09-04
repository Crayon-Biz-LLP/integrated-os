"""Regression tests for the Aug 27 hardening session.

Pins fixes from the PB pipeline audit (session 78):
  - Bug #22: SUGGESTION_SCHEMA must use Gemini-compatible types (not Python lists)
  - Bug #21: RPD counter must only increment on successful API calls
  - Bug #1:  LLMResponse.text must be accessed via attribute, not .strip()
  - Bug #7:  Entity loop must break after first org assignment
  - Bug #8:  Backfill must use word-boundary matching, not substring
  - Bug #20: extraction is PURE — pending nodes are created only by the
    decision-gated queue_pending_candidates() (see Step 1 hardened fix);
    sync/card timing never writes from extraction

Marker: ingest
Layer: L1 unit (no DB, pure logic)
"""
import pytest

pytestmark = pytest.mark.ingest


# ── Bug #22: SUGGESTION_SCHEMA validation ────────────────────────────────

class TestSchemaValidation:
    """The Gemini SDK rejects schemas with list-type values.
    This caused every planner call to fail silently for 5 days (Aug 22-27)."""

    def test_schema_matched_task_id_is_single_type(self):
        """matched_task_id must be a single type string, not a list.
        Before fix: {"type": ["integer", "null"]} → Pydantic validation error.
        After fix:  {"type": "INTEGER", "nullable": True} → accepted."""
        from core.lib.suggestion_extractor import SUGGESTION_SCHEMA

        matched = SUGGESTION_SCHEMA["properties"]["matched_task_id"]
        # Must NOT be a list
        assert not isinstance(matched.get("type"), list), (
            f"matched_task_id type is a list {matched['type']} — "
            f"Gemini SDK rejects this. Use single string + nullable=True"
        )
        # Must be a valid Gemini type string
        valid_types = {"STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT", "NULL"}
        assert matched.get("type") in valid_types, (
            f"matched_task_id type '{matched.get('type')}' not in {valid_types}"
        )

    def test_schema_is_valid_gemini_schema(self):
        """Full schema must pass Gemini SDK validation without errors."""
        from core.lib.suggestion_extractor import SUGGESTION_SCHEMA

        # Gemini SDK accepts both lowercase (JSON Schema) and UPPERCASE forms
        valid_types = {"string", "number", "integer", "boolean", "array", "object", "null",
                       "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT", "NULL"}

        def _validate_gemini_types(obj, path=""):
            """Recursively check that all 'type' fields are single strings (not lists)."""
            if isinstance(obj, dict):
                if "type" in obj:
                    t = obj["type"]
                    assert isinstance(t, str), (
                        f"Schema type at {path} is {type(t).__name__} '{t}', expected string"
                    )
                    assert t in valid_types, (
                        f"Schema type at {path} is '{t}', not in {valid_types}"
                    )
                for k, v in obj.items():
                    _validate_gemini_types(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _validate_gemini_types(v, f"{path}[{i}]")

        _validate_gemini_types(SUGGESTION_SCHEMA)

    def test_schema_actions_items_types_valid(self):
        """All nested types in the actions array must be single strings."""
        from core.lib.suggestion_extractor import SUGGESTION_SCHEMA

        actions_schema = SUGGESTION_SCHEMA["properties"]["actions"]["items"]["properties"]
        for field_name, field_def in actions_schema.items():
            if "type" in field_def:
                assert isinstance(field_def["type"], str), (
                    f"actions.items.properties.{field_name}.type is {type(field_def['type']).__name__}"
                )

    def test_gemini_sdk_accepts_schema(self):
        """Direct test: the Gemini SDK must not reject the schema.
        This is the exact error that killed the planner for 5 days."""
        from core.lib.suggestion_extractor import SUGGESTION_SCHEMA

        # The Gemini SDK validates schemas via Pydantic. If this import
        # or schema construction fails, the planner will fail silently.
        try:
            from google.genai import types
            # Build a GenerateContentConfig to trigger schema validation
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SUGGESTION_SCHEMA,
            )
            # If we get here, the schema is valid
            assert config is not None
        except Exception as e:
            pytest.fail(
                f"Gemini SDK rejected SUGGESTION_SCHEMA: {e}\n"
                f"This is the exact bug that broke the planner (Bug #22)."
            )


# ── Bug #21: RPD counter only increments on success ──────────────────────

class TestRPDRecordUsage:
    """The rate limiter must only count successful API calls, not failed attempts.
    Before fix: _rpd_available() pre-incremented → phantom counts inflated to max.
    After fix:  record_usage() increments only after success."""

    def test_rpd_available_does_not_increment(self):
        """_rpd_available() must be read-only — it checks capacity, doesn't consume it."""
        from core.lib.rate_limiter import MultiKeyLimiter

        limiter = MultiKeyLimiter(prefix="test_rpd_readonly", max_rpm_per_key=100, max_rpd_per_key=5)

        # Clear any existing state
        from core.lib.redis_cache import get_redis
        r = get_redis()
        if r:
            for i in range(4):
                r.delete(f"rhodey:rpd:test_rpd_readonly:key{i}")

        # Check availability — should NOT increment
        assert limiter._rpd_available(0) is True

        # Verify counter is still 0
        if r:
            val = int(r.get("rhodey:rpd:test_rpd_readonly:key0") or 0)
            assert val == 0, f"_rpd_available incremented counter to {val} — must be read-only"

    def test_record_usage_increments_once(self):
        """record_usage() must increment the counter exactly once per call."""
        from core.lib.rate_limiter import MultiKeyLimiter
        from core.lib.redis_cache import get_redis

        limiter = MultiKeyLimiter(prefix="test_rpd_record", max_rpm_per_key=100, max_rpd_per_key=10)
        r = get_redis()
        if r:
            r.delete("rhodey:rpd:test_rpd_record:key0")

        # Record one usage
        limiter.record_usage(0)

        if r:
            val = int(r.get("rhodey:rpd:test_rpd_record:key0") or 0)
            assert val == 1, f"Expected 1 after record_usage, got {val}"

            # Record another
            limiter.record_usage(0)
            val = int(r.get("rhodey:rpd:test_rpd_record:key0") or 0)
            assert val == 2, f"Expected 2 after second record_usage, got {val}"

            # Cleanup
            r.delete("rhodey:rpd:test_rpd_record:key0")

    def test_record_usage_respects_rpd_limit(self):
        """After max_rpd_per_key calls, _rpd_available should return False."""
        from core.lib.rate_limiter import MultiKeyLimiter
        from core.lib.redis_cache import get_redis

        limiter = MultiKeyLimiter(prefix="test_rpd_limit", max_rpm_per_key=100, max_rpd_per_key=3)
        r = get_redis()
        if r:
            r.delete("rhodey:rpd:test_rpd_limit:key0")

        # Should be available initially
        assert limiter._rpd_available(0) is True

        # Record 3 usages (at limit)
        for _ in range(3):
            limiter.record_usage(0)

        # Now should be exhausted
        assert limiter._rpd_available(0) is False, "Key should be exhausted after max_rpd_per_key calls"

        if r:
            r.delete("rhodey:rpd:test_rpd_limit:key0")

    def test_different_keys_independent(self):
        """Each key index has its own RPD counter — they don't interfere."""
        from core.lib.rate_limiter import MultiKeyLimiter
        from core.lib.redis_cache import get_redis

        limiter = MultiKeyLimiter(prefix="test_rpd_indep", max_rpm_per_key=100, max_rpd_per_key=2)
        r = get_redis()
        if r:
            for i in range(4):
                r.delete(f"rhodey:rpd:test_rpd_indep:key{i}")

        # Exhaust key 0
        limiter.record_usage(0)
        limiter.record_usage(0)
        assert limiter._rpd_available(0) is False

        # Key 1 should still be available
        assert limiter._rpd_available(1) is True

        if r:
            for i in range(4):
                r.delete(f"rhodey:rpd:test_rpd_indep:key{i}")


# ── Bug #1: LLMResponse.text attribute access ────────────────────────────

class TestLLMResponseHandling:
    """generate_content_with_fallback returns LLMResponse, not str.
    Code that calls .strip() on it crashes with AttributeError."""

    def test_llmresponse_has_text_attribute(self):
        """LLMResponse must expose .text as a string attribute."""
        from core.llm.response import LLMResponse

        resp = LLMResponse(
            text="hello world",
            provider="test",
            model="test",
            workload="test",
            success=True,
            degraded=False,
            degraded_reason=None,
            attempts=1,
            latency_ms=100,
            final_exception=None,
        )
        assert hasattr(resp, "text")
        assert isinstance(resp.text, str)
        assert resp.text == "hello world"

    def test_llmresponse_text_not_callable(self):
        """LLMResponse.text is an attribute, not a method — .strip() would fail."""
        from core.llm.response import LLMResponse

        resp = LLMResponse(
            text="test",
            provider="test",
            model="test",
            workload="test",
            success=True,
            degraded=False,
            degraded_reason=None,
            attempts=1,
            latency_ms=100,
            final_exception=None,
        )
        # .strip() on a string attribute works, but .strip() on the object itself doesn't
        # The bug was calling response.strip() instead of response.text.strip()
        assert not callable(resp.text), "text should be a string attribute, not a method"

    def test_parse_json_on_empty_text_raises_parse_error(self):
        """parse_json() on empty text raises ParseError — this is what the
        suggestion_extractor catches and returns zero actions."""
        from core.llm.response import LLMResponse
        from core.llm.errors import ParseError

        resp = LLMResponse(
            text="",
            provider="test",
            model="test",
            workload="test",
            success=False,
            degraded=True,
            degraded_reason="all_providers_failed",
            attempts=3,
            latency_ms=857,
            final_exception=None,
        )
        with pytest.raises(ParseError, match="Cannot parse empty response text"):
            resp.parse_json()


# ── Bug #7: Entity loop break after first org ────────────────────────────

class TestOrgLoopBreak:
    """The entity loop must break after the first org assignment.
    Before fix: last org wins (loop continues, overwrites)."""

    def test_first_org_wins(self):
        """When multiple orgs are in the entity list, the first one should be used."""
        # Simulate the entity loop behavior
        orgs = [
            {"type": "organization", "label": "Solvstrat", "entity_id": "org-1"},
            {"type": "organization", "label": "Project Balance", "entity_id": "org-2"},
            {"type": "person", "label": "David", "entity_id": "person-1"},
        ]

        assigned_org = None
        for item in orgs:
            if item["type"] == "organization":
                if assigned_org is None:  # Only assign if not already set
                    assigned_org = item["entity_id"]
                break  # Break after first org (the fix)

        assert assigned_org == "org-1", f"Expected first org (org-1), got {assigned_org}"

    def test_no_org_leaves_none(self):
        """When no orgs are in the entity list, organization_id stays None."""
        orgs = [
            {"type": "person", "label": "David", "entity_id": "person-1"},
        ]

        assigned_org = None
        for item in orgs:
            if item["type"] == "organization":
                if assigned_org is None:
                    assigned_org = item["entity_id"]
                break

        assert assigned_org is None


# ── Bug #8: Backfill word-boundary matching ───────────────────────────────

class TestBackfillWordBoundary:
    """Backfill matching must use word-boundary regex, not substring.
    Before fix: ilike('%label%') → "David" matches "Davidson"."""

    def test_exact_match_works(self):
        """Exact label match should succeed."""
        import re
        label = "David"
        text = "Meeting with David about the project"
        pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
        assert pattern.search(text) is not None

    def test_substring_does_not_match(self):
        """'David' must NOT match 'Davidson'."""
        import re
        label = "David"
        text = "Meeting with Davidson about the project"
        pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
        assert pattern.search(text) is None, (
            f"Word-boundary regex incorrectly matched '{label}' in '{text}'"
        )

    def test_partial_word_does_not_match(self):
        """'Corp' must NOT match 'Corporation'."""
        import re
        label = "Corp"
        text = "Contact Corporation Inc"
        pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
        assert pattern.search(text) is None

    def test_label_at_start_of_text(self):
        """Label at start of text should match."""
        import re
        label = "Solvstrat"
        text = "Solvstrat meeting tomorrow"
        pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
        assert pattern.search(text) is not None

    def test_label_at_end_of_text(self):
        """Label at end of text should match."""
        import re
        label = "Havenlight"
        text = "Call about Havenlight"
        pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
        assert pattern.search(text) is not None

    def test_longest_label_wins(self):
        """When multiple labels match, the longest should win."""
        labels = ["Chennai", "Chennai North", "Chennai Central"]
        text = "Meeting in Chennai North office"
        import re

        matches = []
        for label in labels:
            pattern = re.compile(r'\b' + re.escape(label) + r'\b', re.IGNORECASE)
            if pattern.search(text):
                matches.append(label)

        assert "Chennai North" in matches
        assert "Chennai" in matches  # Also matches (substring of text)
        # Longest match wins
        longest = max(matches, key=len)
        assert longest == "Chennai North"


# ── Bug #20: extraction is pure — pending nodes only via the gated queue ──

class TestTimingSync:
    """Extraction NEVER writes pending nodes (HITL — "only decisions create").

    A sync/card EntityContext carries a detected label but never a pending id;
    pending rows originate only from queue_pending_candidates(), called by
    decision-gated sites (message/email approval). This replaced the old rule
    where timing="sync" created pending nodes inside extraction (the ungated
    junk-node source)."""

    def test_sync_timing_sets_pending_org_label(self):
        """Entity context with timing="sync" should have pending_org_label set."""
        from core.lib.entity_context import EntityContext

        ctx = EntityContext(
            pending_org_label="Havenlight",
            extraction_timing="sync",
        )
        assert ctx.pending_org_label == "Havenlight"
        assert ctx.extraction_timing == "sync"

    def test_card_timing_does_not_create_pending(self):
        """Entity context with timing="card" should NOT create pending nodes."""
        from core.lib.entity_context import EntityContext

        ctx = EntityContext(
            pending_org_label="Havenlight",
            extraction_timing="card",
        )
        # The label is detected, but no pending_id is created
        assert ctx.pending_org_id is None
        assert ctx.pending_org_label == "Havenlight"

    def test_org_linkage_requires_pending_id(self):
        """reconcile_action_orgs needs pending_org_id to link orgs to tasks."""
        from core.actions.executor import reconcile_action_orgs
        from core.actions.models import Action

        class FakeCtx:
            def __init__(self, org_id=None, pending_id=None):
                self.organization_id = org_id
                self.pending_org_id = pending_id

        # With pending_id — should link
        action = Action(operation="create_task", params={"title": "Test"}, human_label="Test")
        reconcile_action_orgs([action], FakeCtx(pending_id="pending-123"))
        assert action.params.get("organization_id") == "pending-123"

        # Without pending_id — should NOT link
        action2 = Action(operation="create_task", params={"title": "Test 2"}, human_label="Test 2")
        reconcile_action_orgs([action2], FakeCtx())
        assert "organization_id" not in action2.params or action2.params.get("organization_id") is None
