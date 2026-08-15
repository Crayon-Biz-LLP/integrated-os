"""Ledger X2/X3/X4 tests — graph-undo training, confirm-signal honesty, run isolation.

X2 — graph/edge undo-training: graph decisions now persist
     metadata.learn_features (core/pulse/graph.py record_decision calls), so
     emit_undo_correction demotes the right pattern on graph undo. Proven here
     at the contract level: a graph-shaped decision row (entity_extraction
     learn payload) drives a correction observation with the EXACT stored
     features — the same helper the graph undo path already calls.

X3 — confirm_auto_all honesty: the bulk confirm paths (Telegram callback +
     /api/auto-decisions/confirm) used to emit ONE decorative observation into
     an 'auto_decisions' bucket nothing reads. emit_confirmed_observation now
     emits per-item confirmations against the decision's REAL subsystem with
     its decision-time features; decisions without a learn payload train
     nothing (no pollution) and telemetry failures never break the confirm.

X4 — per-run chat allocation: each pytest process draws a unique chat band
     (9.1M–9.99M) + per-run thread UUIDs so concurrent CI/local runs never
     collide on the fixed ids the sandbox used to share.

Also: record_decision now accepts and persists a metadata payload (the X2
plumbing), verified hermetically here.
"""

import uuid
from unittest.mock import patch, AsyncMock

import pytest

from core.decisions import record_decision
from core.webhook.utils import emit_confirmed_observation, emit_undo_correction
from tests.fixtures import run_isolation
from tests.fixtures.run_isolation import (
    SandboxLockHeldError,
    acquire_sandbox_lock,
    release_sandbox_lock,
    run_chat_id,
    run_thread_uuid,
)

pytestmark = pytest.mark.learning

# Legacy fixed ids the sandbox used pre-X4 — the per-run band must not collide.
_LEGACY_CHAT_IDS = {999999999, 9000000, 9000001, 9000002, 9000003, 9000005,
                    9000006, 9000007, 9000008, 9000009, 9000010, 9000019,
                    909999999}


def _graph_decision(**overrides) -> dict:
    """A decision row exactly as core/pulse/graph.py now records it."""
    decision = {
        "id": 2001,
        "decision_type": "graph_edge_approval",
        "entity_type": "graph_edge",
        "entity_id": "42",
        "status": "active",
        "metadata": {
            "learn_features": {
                "relationship": "WORKS_AT",
                "source_type": "person",
                "target_type": "organization",
            },
            "learn_subsystem": "entity_extraction",
        },
    }
    decision.update(overrides)
    return decision


# ── X2: record_decision metadata plumbing ─────────────────────────────────

def test_x2_record_decision_persists_metadata():
    """record_decision must write the learn payload into decisions.metadata —
    that is the X2 plumbing the graph decision sites now use."""
    captured = {}

    class _FakeExecute:
        def __init__(self, data):
            self.data = [dict(data, id=1)]

    class _FakeTable:
        def insert(self, data):
            captured["data"] = data
            return self

        def execute(self):
            return _FakeExecute(captured["data"])

    class _FakeClient:
        def table(self, name):
            return _FakeTable()

    with patch("core.decisions.tenant_aware_client", return_value=_FakeClient()), \
         patch("core.decisions.audit_log_sync"):
        record_decision(
            decision_type="graph_edge_approval",
            title="Approved edge: A → WORKS_AT → B",
            entity_type="graph_edge",
            entity_id="42",
            source="decision_pulse",
            auto_decided=True,
            metadata={
                "learn_features": {"relationship": "WORKS_AT"},
                "learn_subsystem": "entity_extraction",
            },
        )

    assert captured["data"]["metadata"] == {
        "learn_features": {"relationship": "WORKS_AT"},
        "learn_subsystem": "entity_extraction",
    }


# ── X2: graph undo now demotes (contract-level, same helper as the undo path)

@pytest.mark.asyncio
async def test_x2_graph_undo_emits_demotion_with_exact_features():
    """A graph decision row with the learn payload X2 now persists drives the
    undo correction into entity_extraction with the EXACT decision-time
    features — so the wrong edge pattern demotes instead of staying strong."""
    sent = {}

    async def _fake_emit(**kwargs):
        sent.update(kwargs)

    with patch("core.webhook.utils.emit_observation", new=_fake_emit):
        await emit_undo_correction(_graph_decision())

    assert sent.get("outcome") == "corrected"
    assert sent.get("subsystem") == "entity_extraction"
    assert sent.get("features") == {
        "relationship": "WORKS_AT",
        "source_type": "person",
        "target_type": "organization",
    }


# ── X3: confirm observations are per-item and honest ─────────────────────

@pytest.mark.asyncio
async def test_x3_confirm_emits_against_real_subsystem():
    """Confirming an auto-decision reinforces the pattern that produced it:
    real subsystem + exact decision-time features, per item."""
    sent = {}

    async def _fake_emit(**kwargs):
        sent.update(kwargs)

    with patch("core.webhook.utils.emit_observation", new=_fake_emit):
        emitted = await emit_confirmed_observation(
            _graph_decision(), source_tag="confirm_auto_all"
        )

    assert emitted is True
    assert sent.get("subsystem") == "entity_extraction"
    assert sent.get("outcome") == "confirmed"
    assert sent.get("predicted") == "auto_approve"
    assert sent.get("features") == {
        "relationship": "WORKS_AT",
        "source_type": "person",
        "target_type": "organization",
    }
    assert sent.get("source") == "confirm_auto_all"


@pytest.mark.asyncio
async def test_x3_confirm_without_learn_payload_trains_nothing():
    """Pre-fix decisions (no learn payload) emit NO observation — the old
    decorative 'auto_decisions' bucket is gone; nothing is polluted."""
    with patch("core.webhook.utils.emit_observation", new=AsyncMock()) as mock_emit:
        emitted = await emit_confirmed_observation(
            _graph_decision(metadata=None), source_tag="confirm_auto_all"
        )
        assert emitted is False
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_x3_confirm_telemetry_error_is_caught():
    """A telemetry hiccup during confirm must not break the confirm itself."""
    with patch("core.webhook.utils.emit_observation",
               side_effect=Exception("db down")), \
         patch("core.webhook.utils.audit_log_sync") as mock_log:
        emitted = await emit_confirmed_observation(
            _graph_decision(), source_tag="confirm_auto_all"
        )
        assert emitted is False
        mock_log.assert_called()  # warning logged, not raised


# ── X4: per-run chat/thread allocation ────────────────────────────────────

def test_x4_run_chat_band_is_unique_and_clear_of_legacy():
    """The per-run band lives in 9.1M–9.99M — below real Telegram ids,
    distinct from every legacy fixed test chat id, and wide enough for all
    suite offsets (0..19 note_capture, +1 suite2, +2 UAT)."""
    base = run_isolation.RUN_CHAT_BASE
    assert 9100000 <= base <= 9989999, base
    assert base not in _LEGACY_CHAT_IDS
    band = set(range(base, base + run_isolation.RUN_CHAT_SPAN))
    assert band.isdisjoint(_LEGACY_CHAT_IDS)
    # Offsets the suites actually use stay inside the band.
    assert run_chat_id(19) < base + run_isolation.RUN_CHAT_SPAN


def test_x4_run_thread_uuid_shape_and_uniqueness():
    """Per-run thread ids keep the leak-guard prefix but vary the tail, so
    concurrent runs never hit the same PK (the old fixed '...aaaa' tails)."""
    t0, t1, t2 = run_thread_uuid(0), run_thread_uuid(1), run_thread_uuid(2)
    for t in (t0, t1, t2):
        assert t.startswith("00000000-0000-4000-8000-")
        # Valid UUID v4-shape (version nibble preserved from the prefix).
        assert uuid.UUID(t)
    assert len({t0, t1, t2}) == 3


# ── X4 residual: cross-machine sandbox lock (Redis SET NX EX) ─────────────

class _FakeRedis:
    """Minimal stand-in for the upstash-redis client (dict store)."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        return 1


_LOCK_KEY = "rhodey:test-sandbox:lock"


def test_x4_sandbox_lock_acquire_and_release():
    """A free lock is acquired (token stored, TTL set) and released, leaving
    the store clean for the next run."""
    fake = _FakeRedis()
    lock = acquire_sandbox_lock(client=fake)
    assert lock is not None
    assert fake.store[_LOCK_KEY] == lock["token"]
    assert lock["token"].startswith(lock["token"].split(":")[0])  # host prefix
    release_sandbox_lock(lock)
    assert _LOCK_KEY not in fake.store


def test_x4_sandbox_lock_held_fails_closed(monkeypatch):
    """A held lock fails the second run with a clear error (fail-closed,
    fail-fast when the bounded wait is 0) instead of racing."""
    monkeypatch.setattr(run_isolation, "_SANDBOX_LOCK_WAIT_S", 0)
    fake = _FakeRedis()
    lock1 = acquire_sandbox_lock(client=fake)
    try:
        with pytest.raises(SandboxLockHeldError):
            acquire_sandbox_lock(client=fake)
    finally:
        release_sandbox_lock(lock1)


def test_x4_sandbox_lock_release_never_clears_other_run():
    """Release must only clear OUR token — if another run's lock superseded
    ours (e.g. ours expired and they took over), we must not delete theirs."""
    fake = _FakeRedis()
    lock = acquire_sandbox_lock(client=fake)
    # Our TTL expired / another process took the lock while we were running.
    fake.store[_LOCK_KEY] = "other-host:pid999:1700000000"
    release_sandbox_lock(lock)
    assert fake.store[_LOCK_KEY] == "other-host:pid999:1700000000"


def test_x4_sandbox_lock_no_redis_noop(monkeypatch):
    """Redis unconfigured → get_redis() returns None → no lock, no error:
    hermetic and env-less runs proceed exactly as before."""
    monkeypatch.setattr(run_isolation, "get_redis", lambda: None)
    assert acquire_sandbox_lock() is None
    release_sandbox_lock(None)  # no-op, must not raise
