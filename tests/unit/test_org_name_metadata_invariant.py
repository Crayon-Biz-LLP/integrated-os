"""
Unit tests for the hardened org-name/metadata invariant (Plumfleet/Qhord class).

Pins: note metadata stores ONLY the resolved organization_id — never a second
copy of the org NAME that can diverge from the id (historically the planner's
caller-supplied string "Qhord" was stamped next to the resolved Plumfleet id).

Invariants:
  1. create_note_direct never writes metadata['organization_name'] — even when
     the caller passes an org name that CONFLICTS with the resolved ctx id.
  2. The DB column organization_id always equals the resolved ctx id.
  3. A thread anchor can never override a resolved org (anchor gate keys on the
     RESOLVED result, not the caller param).
  4. extra_metadata can never override the resolved organization_id.

Marker: graph
Layer: L1 unit (no DB, mocked client)
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.lib.entity_context import EntityContext

# ── Aspect marker ──────────────────────────────────────────────────────────────
pytestmark = [pytest.mark.graph]


class FakeEmbedding:
    def __init__(self):
        self.vector = [0.1, 0.2, 0.3]


class _FakeResp:
    def __init__(self, data):
        self.data = data


def _make_supabase(insert_resp=None):
    """MagicMock supabase whose memories.insert returns insert_resp."""
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value = (
        insert_resp if insert_resp is not None else _FakeResp([{"id": 9001}])
    )
    return sb


def _run_create_note(supabase, entity_context=None, organization_name=None,
                     active_anchor=None, extra_metadata=None):
    """Call the REAL create_note_direct with all side channels mocked."""
    import core.lib.enrichment_queue as eq_mod
    import core.lib.time_utils as time_mod
    from core.pulse import tools as tools_mod
    with ExitStack() as stack:
        stack.enter_context(patch.object(tools_mod, "supabase", supabase))
        stack.enter_context(patch.object(tools_mod, "audit_log_sync", MagicMock()))
        stack.enter_context(patch.object(tools_mod, "get_embedding",
                                         AsyncMock(return_value=FakeEmbedding())))
        stack.enter_context(patch.object(eq_mod, "enqueue_enrichment", MagicMock()))
        stack.enter_context(patch.object(tools_mod, "schedule_index_memory", MagicMock()))
        stack.enter_context(patch.object(tools_mod, "accumulate_action", MagicMock()))
        stack.enter_context(patch.object(time_mod, "compute_expires_at",
                                         MagicMock(return_value=None)))
        return asyncio.run(tools_mod.create_note_direct(
            content="Plumfleet has become a dead lead",
            source="web",
            organization_name=organization_name,
            entity_context=entity_context,
            active_anchor=active_anchor,
            extra_metadata=extra_metadata,
        ))


class TestNoteMetadataOrgNameInvariant:
    """metadata.organization_name must never exist; id always follows resolution."""

    def test_no_org_name_when_ctx_resolves_org_and_caller_passes_conflicting_name(self):
        # The exact Plumfleet/Qhord repro: ctx resolves Plumfleet (id + name),
        # caller-supplied string says "Qhord".
        ctx = EntityContext(
            organization_id="28c9e7ad-931e-44e6-ba43-e5364f6342d0",
            organization_name="Plumfleet",
        )
        sb = _make_supabase()
        result = _run_create_note(sb, entity_context=ctx, organization_name="Qhord")

        assert result["action"] == "filed"
        insert_data = sb.table.return_value.insert.call_args[0][0]
        # Column: resolved id wins.
        assert insert_data["organization_id"] == "28c9e7ad-931e-44e6-ba43-e5364f6342d0"
        metadata = insert_data["metadata"]
        # The divergent name is never stored.
        assert "organization_name" not in metadata
        # The id stored in metadata matches the column (single source of truth).
        assert metadata["organization_id"] == insert_data["organization_id"]

    def test_no_org_name_when_no_org_resolved_but_caller_passes_name(self):
        # No ctx org: caller's name never lands in metadata as a name field.
        sb = _make_supabase()
        result = _run_create_note(sb, entity_context=EntityContext(),
                                  organization_name="Qhord")

        assert result["action"] == "filed"
        insert_data = sb.table.return_value.insert.call_args[0][0]
        assert insert_data.get("organization_id") is None
        metadata = insert_data.get("metadata") or {}
        assert "organization_name" not in metadata

    def test_anchor_cannot_override_resolved_org(self):
        # Door-2 repro: ctx resolves Plumfleet, caller passes no org id, but the
        # thread anchor carries Qhord's last_org_id. Anchor must NOT override.
        ctx = EntityContext(
            organization_id="28c9e7ad-931e-44e6-ba43-e5364f6342d0",
            organization_name="Plumfleet",
        )
        anchor = {
            "name": "Qhord",
            "type": "organization",
            "last_org_id": "27f5eb4d-225c-4ce0-871b-230cbe13f904",
        }
        sb = _make_supabase()
        result = _run_create_note(sb, entity_context=ctx, active_anchor=anchor)

        assert result["action"] == "filed"
        insert_data = sb.table.return_value.insert.call_args[0][0]
        assert insert_data["organization_id"] == "28c9e7ad-931e-44e6-ba43-e5364f6342d0"
        metadata = insert_data["metadata"]
        assert metadata["organization_id"] == "28c9e7ad-931e-44e6-ba43-e5364f6342d0"
        assert "organization_name" not in metadata

    def test_anchor_fallback_used_only_when_nothing_resolved(self):
        # No ctx org at all: anchor is the last-resort fallback (provenance id,
        # still no name field).
        anchor = {
            "name": "Qhord",
            "type": "organization",
            "last_org_id": "27f5eb4d-225c-4ce0-871b-230cbe13f904",
        }
        sb = _make_supabase()
        result = _run_create_note(sb, entity_context=EntityContext(),
                                  active_anchor=anchor)

        assert result["action"] == "filed"
        insert_data = sb.table.return_value.insert.call_args[0][0]
        metadata = insert_data["metadata"]
        assert metadata.get("organization_id") == "27f5eb4d-225c-4ce0-871b-230cbe13f904"
        assert "organization_name" not in metadata
        # Provenance labels preserved.
        assert metadata["thread_entity_name"] == "Qhord"

    def test_extra_metadata_cannot_override_resolved_org_id(self):
        ctx = EntityContext(
            organization_id="28c9e7ad-931e-44e6-ba43-e5364f6342d0",
            organization_name="Plumfleet",
        )
        sb = _make_supabase()
        result = _run_create_note(
            sb,
            entity_context=ctx,
            extra_metadata={"intent": "NOTE",
                            "organization_id": "27f5eb4d-225c-4ce0-871b-230cbe13f904"},
        )

        assert result["action"] == "filed"
        insert_data = sb.table.return_value.insert.call_args[0][0]
        metadata = insert_data["metadata"]
        assert metadata["organization_id"] == "28c9e7ad-931e-44e6-ba43-e5364f6342d0"
        assert metadata["intent"] == "NOTE"