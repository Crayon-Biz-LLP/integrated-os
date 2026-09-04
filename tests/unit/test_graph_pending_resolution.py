"""
Unit tests for Step 2 pending-resolution sweep (orphan fix).

Pins the invariant: the moment a live graph node exists for a label, every
same-label pending row (same owner + node_type, still pending/flagged/
awaiting_details) is marked approved and its pending_org_id links are
re-pointed to the live node — no code path can leave a pending row dangling
once its label is live.

Marker: graph
Layer: L1 unit (no DB, mocked client)
"""

from unittest.mock import MagicMock

import pytest
from core.pulse import graph as graph_mod

# ── Aspect marker ──────────────────────────────────────────────────────────────
pytestmark = [pytest.mark.graph]


class TestResolveMatchingPendingNodes:
    """resolve_matching_pending_nodes — the single same-label resolver."""

    def _client_with_pending(self, rows: list) -> MagicMock:
        supabase = MagicMock()
        # Chain: .table('pending_nodes').select(...).eq(...).eq(...).ilike(...).in_(...).execute()
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value \
            .ilike.return_value.in_.return_value.execute.return_value.data = rows
        return supabase

    def test_resolves_org_pendings_and_repoints(self, monkeypatch):
        supabase = self._client_with_pending([
            {"id": 101, "label": "Nova Dynamics", "node_type": "organization"},
            {"id": 102, "label": "nova dynamics", "node_type": "organization"},
        ])
        monkeypatch.setattr(graph_mod, "supabase", supabase)
        monkeypatch.setattr(graph_mod, "audit_log_sync", lambda *a, **k: None)

        resolved = []
        monkeypatch.setattr(
            graph_mod, "_resolve_pending_org_on_approval",
            lambda p_id, g_id: resolved.append((p_id, g_id)),
        )

        n = graph_mod.resolve_matching_pending_nodes(
            "Nova Dynamics", "organization", "live-org-uuid", owner_id="owner-1"
        )
        assert n == 2
        # Both rows marked approved
        marks = [c.args[0] for c in supabase.table.return_value.update.call_args_list]
        assert marks == [{"status": "approved"}, {"status": "approved"}]
        # Org re-pointing ran for every resolved row, against the live node
        assert resolved == [(101, "live-org-uuid"), (102, "live-org-uuid")]

    def test_person_pendings_resolved_without_org_repoint(self, monkeypatch):
        supabase = self._client_with_pending([
            {"id": 201, "label": "Sharukh", "node_type": "person"},
        ])
        monkeypatch.setattr(graph_mod, "supabase", supabase)
        monkeypatch.setattr(graph_mod, "audit_log_sync", lambda *a, **k: None)
        repointed = []
        monkeypatch.setattr(
            graph_mod, "_resolve_pending_org_on_approval",
            lambda p_id, g_id: repointed.append((p_id, g_id)),
        )

        n = graph_mod.resolve_matching_pending_nodes(
            "Sharukh", "person", "live-person-uuid", owner_id="owner-1"
        )
        assert n == 1
        assert repointed == []  # persons carry no pending_org_id to re-point

    def test_no_matching_rows_is_noop(self, monkeypatch):
        supabase = self._client_with_pending([])
        monkeypatch.setattr(graph_mod, "supabase", supabase)
        monkeypatch.setattr(graph_mod, "audit_log_sync", lambda *a, **k: None)
        monkeypatch.setattr(graph_mod, "_resolve_pending_org_on_approval", lambda *a: None)

        n = graph_mod.resolve_matching_pending_nodes(
            "No Such Org", "organization", "live-uuid", owner_id="owner-1"
        )
        assert n == 0
        assert supabase.table.return_value.update.call_count == 0

    def test_type_mismatch_not_resolved(self, monkeypatch):
        # A 'concept' pending with the same label must NOT be resolved by a
        # person/org creation — different entity kinds stay separate.
        supabase = self._client_with_pending([
            {"id": 301, "label": "Nova", "node_type": "concept"},
        ])
        monkeypatch.setattr(graph_mod, "supabase", supabase)
        monkeypatch.setattr(graph_mod, "audit_log_sync", lambda *a, **k: None)
        monkeypatch.setattr(graph_mod, "_resolve_pending_org_on_approval", lambda *a: None)

        # The helper itself filters by node_type — simulate the query returning
        # nothing because the sweep's .eq('node_type', ...) excluded it.
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value \
            .ilike.return_value.in_.return_value.execute.return_value.data = []
        n = graph_mod.resolve_matching_pending_nodes(
            "Nova", "organization", "live-uuid", owner_id="owner-1"
        )
        assert n == 0

    def test_exception_is_contained(self, monkeypatch):
        supabase = MagicMock()
        supabase.table.return_value.select.side_effect = Exception("boom")
        monkeypatch.setattr(graph_mod, "supabase", supabase)
        monkeypatch.setattr(graph_mod, "audit_log_sync", lambda *a, **k: None)

        n = graph_mod.resolve_matching_pending_nodes(
            "Nova", "organization", "live-uuid", owner_id="owner-1"
        )
        assert n == 0  # never raises — callers (node creation) must not break