"""Learning-loop END tests — the vision-#4 behavior delta (no DB required).

Covers the loop as a two-phase contract:
  Phase A (persist): a decision is recorded → emit_observation() upserts the
  subsystem_patterns rolling counter (via _update_pattern_count).
  Phase B (re-run → behavior change): compute_pattern_confidence() on the
  SAME features now returns a different recommendation than before the
  decisions — the "learns from every decision" promise.

Tests:
  L1 — escalation boundary: the 3rd approved observation flips review → approve
        (MIN_PATTERN_OBSERVATIONS crossing — the exact learning moment).
  L2 — demotion: corrected/rejected observations push error_rate past
        MAX_ERROR_RATE and demote a previously-approving pattern.
  L3 — the two-phase loop: emit 3 approved observations (stateful fake) →
        re-run confidence on the same features → approve. And the reverse:
        rejections flip it back to review.
  L4 — undo trains: emit_undo_correction re-emits the inverse observation
        with the EXACT stored features (approval-undo → corrected).
  L5 — fail-open: undo correction never breaks on missing payload/DB errors.

The stateful fake client accumulates subsystem_patterns rows the way the
real DB does (select-then-insert-or-update), so emit → compute exercises the
real code path end to end without a database.
"""

import pytest
from datetime import datetime, timezone, timedelta

from unittest.mock import patch, AsyncMock, MagicMock

from core.lib.telemetry import (
    emit_observation,
    compute_pattern_confidence,
)
from core.webhook.utils import emit_undo_correction
pytestmark = pytest.mark.learning


# Recent timestamps (relative to now) so temporal decay leaves confidence at
# 1.0× — hardcoded dates would drift into the decay window as time passes.
_RECENT = datetime.now(timezone.utc)
_T1_AGO = (_RECENT - timedelta(days=6)).isoformat()
_T2_AGO = (_RECENT - timedelta(days=5)).isoformat()


# ── Stateful fake: subsystem_patterns accumulates like the real DB ──────

class _StatefulClient:
    """Stand-in for tenant_aware_client(): subsystem_patterns rows persist
    across emit → compute, keyed by (subsystem, feature_hash)."""

    def __init__(self):
        self.patterns = {}  # (subsystem, feature_hash) -> row dict
        self.telemetry_rows = []
        self.builders = {}

    def table(self, name):
        # Fresh builder per call — mirrors the real supabase client (every
        # .table() returns a new chain); shared state across calls is what
        # the store handles, not the builder.
        return _Builder(self, name)


class _Builder:
    """Self-chaining builder that resolves against the stateful store."""

    def __init__(self, client, table):
        self._client = client
        self._table = table
        self._eq = {}
        self._payload = None
        self._action = None
        self._limit = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def limit(self, n):
        self._limit = n
        return self

    def maybe_single(self):
        return self

    def insert(self, payload):
        self._action = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._action = "update"
        self._payload = payload
        return self

    def execute(self):
        if self._table == "subsystem_patterns":
            if self._action == "insert":
                key = (self._payload["subsystem"], self._payload["feature_hash"])
                row = dict(self._payload)
                row["id"] = len(self._client.patterns) + 1
                self._client.patterns[key] = row
                return MagicMock(data=[row])
            if self._action == "update":
                # update by id
                for key, row in list(self._client.patterns.items()):
                    if row.get("id") == self._eq.get("id"):
                        row.update(self._payload)
                return MagicMock(data=[])
            # select — return the row for (subsystem, feature_hash)
            key = (self._eq.get("subsystem"), self._eq.get("feature_hash"))
            row = self._client.patterns.get(key)
            return MagicMock(data=row if row is not None else None)
        if self._table == "core_config":
            # suggest_approved override lookup — no rows → no override
            return MagicMock(data=None)
        # subsystem_telemetry insert (no-op capture)
        if self._action == "insert":
            self._client.telemetry_rows.append(self._payload)
        return MagicMock(data=[self._payload] if self._payload is not None else [])


# ── L1: escalation boundary — the 3rd decision flips behavior ───────────

@pytest.mark.asyncio
async def test_l1_escalation_boundary_two_review_three_approve():
    """2 approved observations → review; the 3rd → approve.

    This is the MIN_PATTERN_OBSERVATIONS crossing: Rhodey starts acting on
    the pattern (auto-approving) only after the user has confirmed it 3
    times. The boundary IS the learning moment.
    """
    features = {"source": "email", "node_type": "person"}

    client = _StatefulClient()
    # Two approved observations
    for _ in range(2):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="entity_extraction",
                event_type="approval",
                features=features,
                outcome="confirmed",
            )

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(features, "entity_extraction")
    assert result["recommendation"] != "approve", \
        f"2 obs must NOT auto-approve — got {result['recommendation']}"

    # Third approved observation crosses the boundary
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        await emit_observation(
            subsystem="entity_extraction",
            event_type="approval",
            features=features,
            outcome="confirmed",
        )
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(features, "entity_extraction")
    assert result["recommendation"] == "approve", \
        f"3 obs must auto-approve — got {result['recommendation']}"


# ── L2: demotion — corrections train the pattern down ───────────────────

@pytest.mark.asyncio
async def test_l2_corrections_demote_approved_pattern():
    """After 3 approvals (approve), 2 corrections push error_rate > 0.5 and
    demote the pattern — the same class of item stops being auto-approved.
    This is the "Not now that trains" contract: user pushback is learned.
    """
    features = {"source": "email", "node_type": "person"}
    client = _StatefulClient()

    for _ in range(3):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="entity_extraction",
                event_type="approval",
                features=features,
                outcome="confirmed",
            )
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        before = await compute_pattern_confidence(features, "entity_extraction")
    assert before["recommendation"] == "approve"

    # Two corrections/rejections → error_rate 2/5 = 0.4... need > 0.5
    # (MAX_ERROR_RATE). Three corrections on 3 approvals = 3/6 = 0.5 (not >
    # 0.5 — boundary), so four on three = 4/7 = 0.57 > 0.5 → demoted. Use a
    # steeper mix: 3 approvals + 4 corrections.
    for _ in range(4):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="entity_extraction",
                event_type="rejection",
                features=features,
                outcome="rejected",
            )
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        after = await compute_pattern_confidence(features, "entity_extraction")
    assert after["recommendation"] != "approve", \
        f"corrected pattern must not auto-approve — got {after['recommendation']}"
    # The corrected_count is visible in the store (the demotion signal)
    import core.lib.telemetry as telemetry_mod
    fh = telemetry_mod.hash_features(features, "entity_extraction")
    row = client.patterns.get(("entity_extraction", fh))
    assert row is not None and row["corrected_count"] >= 4


# ── L3: the two-phase loop — persist then re-run (the D4 core) ──────────

@pytest.mark.asyncio
async def test_l3_two_phase_loop_persist_then_behavior_change():
    """Phase A: 3 approved decisions persist. Phase B: re-running confidence
    on the SAME features returns 'approve' (it returned review before any
    decisions). This is the loop END — a persisted decision changes
    subsequent pipeline behavior.
    """
    features = {"source": "telegram", "node_type": "concept"}

    client = _StatefulClient()
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        before = await compute_pattern_confidence(features, "classification")
    assert before["recommendation"] == "review"
    assert before["total_observations"] == 0

    for _ in range(3):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="classification",
                event_type="approval",
                features=features,
                outcome="confirmed",
            )

    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        after = await compute_pattern_confidence(features, "classification")
    assert after["recommendation"] == "approve"
    assert after["total_observations"] == 3


@pytest.mark.asyncio
async def test_l3_rejections_flip_loop_back_to_review():
    """The loop runs both ways: approvals train up, then corrections train
    back down. A pattern that learned 'approve' and then gets corrected hard
    must stop auto-approving.
    """
    features = {"source": "email", "has_url": True}
    client = _StatefulClient()

    for _ in range(3):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="classification",
                event_type="approval",
                features=features,
                outcome="confirmed",
            )
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        assert (await compute_pattern_confidence(features, "classification"))["recommendation"] == "approve"

    for _ in range(4):
        with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
            await emit_observation(
                subsystem="classification",
                event_type="rejection",
                features=features,
                outcome="rejected",
            )
    with patch("core.lib.telemetry.tenant_aware_client", return_value=client):
        result = await compute_pattern_confidence(features, "classification")
    assert result["recommendation"] != "approve"


# ── L4: undo trains — the inverse observation is emitted ────────────────

def _decision(**overrides) -> dict:
    decision = {
        "id": 1001,
        "decision_type": "channel_approval",
        "entity_type": "message",
        "entity_id": "7353",
        "status": "active",
        "metadata": {
            "learn_features": {"source": "email", "sender_name": "acme"},
            "learn_subsystem": "email_pipeline",
            "actions": [],
        },
    }
    decision.update(overrides)
    return decision


@pytest.mark.asyncio
async def test_l4_undo_approval_emits_correction():
    """Undoing an auto-approve re-emits the observation as a CORRECTION with
    the exact stored features — so the pattern that overstepped demotes
    instead of staying strong (the trust-breaker this closes).
    """
    sent = {}

    async def _fake_emit(**kwargs):
        sent.update(kwargs)

    with patch("core.webhook.utils.emit_observation", new=_fake_emit):
        await emit_undo_correction(_decision())

    assert sent.get("outcome") == "corrected"
    assert sent.get("subsystem") == "email_pipeline"
    assert sent.get("features") == {"source": "email", "sender_name": "acme"}
    assert sent.get("source") == "decision_undo"


@pytest.mark.asyncio
async def test_l4_undo_rejection_emits_confirmation():
    """The inverse holds: undoing a rejection re-strengthens the pattern
    (confirmation) — the decision that demoted it is itself reversed.
    """
    sent = {}

    async def _fake_emit(**kwargs):
        sent.update(kwargs)

    with patch("core.webhook.utils.emit_observation", new=_fake_emit):
        await emit_undo_correction(_decision(decision_type="email_rejection"))

    assert sent.get("outcome") == "confirmed"
    assert sent.get("subsystem") == "email_pipeline"


# ── L5: fail-open — undo never breaks on missing/errored payloads ───────

@pytest.mark.asyncio
async def test_l5_undo_without_learn_payload_is_noop():
    """Pre-fix decisions (no metadata.learn_features) can't be corrected —
    the helper must no-op silently, never raise.
    """
    with patch("core.webhook.utils.emit_observation", new=AsyncMock()) as mock_emit:
        await emit_undo_correction(_decision(metadata=None))
        await emit_undo_correction(_decision(metadata={}))
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_l5_undo_emit_error_is_caught():
    """A telemetry hiccup during undo-correction must not break the undo."""
    with patch("core.webhook.utils.emit_observation",
               side_effect=Exception("db down")), \
         patch("core.webhook.utils.audit_log_sync") as mock_log:
        await emit_undo_correction(_decision())
        mock_log.assert_called()  # warning logged, not raised
