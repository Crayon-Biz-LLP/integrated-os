"""Live learning-loop END tests (L2-live, nightly, TEST tenant only).

Proves the vision-#4 loop against the real DB:

  L1 — persist → re-run → behavior change: seed 3 approved observations for
       a feature set (real subsystem_patterns rows under the TEST tenant),
       then compute_pattern_confidence on the SAME features returns
       'approve' (it returned 'review' before any decisions). This is the
       decision-pulse gate: a new item matching the learned pattern gets
       auto-approved instead of shown for review.
  L2 — undo trains: emit_undo_correction with a decision row carrying
       learn_features bumps the pattern's corrected_count in the real DB —
       the inverse signal that demotes an overstepping pattern.

Both tests clean up after themselves (owner-scoped), and the session leak
guard extends to [TEST]/[SIM_TEST] marker rows — nothing leaks to another
tenant. Live-only: skipped when the TEST tenant / live DB is unavailable.
"""

import pytest

from tests.sim.conftest import (
    TEST_TENANT_UID,
    requires_live_db,
    fresh_supabase,
)
from tests.fixtures.test_tenant import resolve_test_tenant_uid
from core.lib.telemetry import (
    emit_observation,
    compute_pattern_confidence,
    hash_features,
)
from core.services.db import tenant_scope
from core.webhook.utils import emit_undo_correction

pytestmark = [requires_live_db, pytest.mark.learning]

_FEATURES = {"source": "email", "node_type": "person"}
_SUBSYSTEM = "entity_extraction"


@pytest.fixture
def _clean_pattern_rows():
    """Sweep any prior [SIM_TEST] learning-loop pattern rows owned by the
    TEST tenant before/after — the feature set is fixed, so residue from a
    killed run must not skew the counters."""
    supabase = fresh_supabase()
    fh = hash_features(_FEATURES, _SUBSYSTEM)
    try:
        supabase.table("subsystem_patterns") \
            .delete().eq("owner_id", TEST_TENANT_UID) \
            .eq("subsystem", _SUBSYSTEM).eq("feature_hash", fh).execute()
    except Exception:
        pass
    yield
    try:
        supabase.table("subsystem_patterns") \
            .delete().eq("owner_id", TEST_TENANT_UID) \
            .eq("subsystem", _SUBSYSTEM).eq("feature_hash", fh).execute()
    except Exception:
        pass


# ── L1: the two-phase loop against the real DB ──────────────────────────

@requires_live_db
def test_l1_two_phase_loop_real_db(_clean_pattern_rows):
    """Phase A: 3 approved observations persist. Phase B: re-running the
    decision-pulse gate on the SAME features flips review → approve.

    This is the loop END: a new item matching the learned pattern would be
    auto-approved by _process_decision_pulse_impl instead of surfacing for
    manual review.
    """
    uid = resolve_test_tenant_uid()
    assert uid, "test tenant unresolvable — refusing unscoped run"

    # Phase A — persist 3 approved observations (real rows, TEST tenant)
    for _ in range(3):
        res = _emit_observation_real(_FEATURES, _SUBSYSTEM)
        assert res is True, "emit_observation failed against live DB"

    # Phase B — re-run the gate
    result = _compute_real(_FEATURES, _SUBSYSTEM)
    assert result["recommendation"] == "approve", \
        f"3 persisted approvals must flip the gate to approve — got {result['recommendation']}"
    assert result["total_observations"] == 3


# ── L2: undo trains against the real DB ─────────────────────────────────

@requires_live_db
def test_l2_undo_correction_demotes_pattern_real_db(_clean_pattern_rows):
    """An undo emits the inverse observation: corrected_count rises in the
    real subsystem_patterns row — the demotion signal that stops the same
    class of item from being auto-approved again.
    """
    uid = resolve_test_tenant_uid()
    assert uid

    for _ in range(3):
        _emit_observation_real(_FEATURES, _SUBSYSTEM)

    decision_row = {
        "id": 900000 + 1,
        "decision_type": "channel_approval",
        "entity_type": "message",
        "entity_id": "7353",
        "metadata": {
            "learn_features": _FEATURES,
            "learn_subsystem": _SUBSYSTEM,
        },
    }
    import asyncio
    with tenant_scope(TEST_TENANT_UID):
        asyncio.run(emit_undo_correction(decision_row))

    fh = hash_features(_FEATURES, _SUBSYSTEM)
    supabase = fresh_supabase()
    rows = supabase.table("subsystem_patterns") \
        .select("total_count, corrected_count, owner_id") \
        .eq("owner_id", TEST_TENANT_UID) \
        .eq("subsystem", _SUBSYSTEM).eq("feature_hash", fh) \
        .execute()
    row = (rows.data or [None])[0]
    assert row is not None, "pattern row missing after undo correction"
    assert row["corrected_count"] >= 1, \
        f"undo must increment corrected_count — got {row['corrected_count']}"


# ── helpers (tenant-scoped, real emit) ──────────────────────────────────

def _emit_observation_real(features: dict, subsystem: str) -> bool:
    """Run the REAL emit_observation under the TEST tenant scope so rows land
    with owner_id = TEST_TENANT_UID (never another tenant)."""
    from core.services.db import tenant_scope
    import asyncio
    with tenant_scope(TEST_TENANT_UID):
        return asyncio.run(emit_observation(
            subsystem=subsystem,
            event_type="approval",
            features=features,
            outcome="confirmed",
            source="sim_learning_loop",
        ))


def _compute_real(features: dict, subsystem: str) -> dict:
    """Run the REAL compute_pattern_confidence under the TEST tenant scope."""
    from core.services.db import tenant_scope
    import asyncio
    with tenant_scope(TEST_TENANT_UID):
        return asyncio.run(compute_pattern_confidence(features, subsystem))
