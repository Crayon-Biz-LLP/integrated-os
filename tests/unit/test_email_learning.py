"""Unit tests for the email draft / decision learning emissions.

Covers `_emit_draft_observation` (core/webhook/email.py) — the helper that
closes the gap where drafts were silently sent/edited/dropped without any
pattern-learning signal. Each draft outcome must map to a distinct telemetry
observation so the pattern learner can tell "shipped as-is" (confirmed) from
"user rewrote it" (corrected) from "discarded" (rejected).

These tests monkeypatch the module-level `emit_observation` so no DB or
network is touched.
"""

import asyncio

from core.webhook.email import _emit_draft_observation


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return True


def _run(coro):
    return asyncio.run(coro)


def test_draft_send_emits_confirmed(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("core.webhook.email.emit_observation", rec)

    _run(_emit_draft_observation("approval", "confirmed", "Sure, sending the deck shortly."))

    assert len(rec.calls) == 1
    obs = rec.calls[0]
    assert obs["subsystem"] == "email_drafts"
    assert obs["event_type"] == "approval"
    assert obs["outcome"] == "confirmed"
    assert obs["predicted"] == "Sure, sending the deck shortly."
    assert obs["actual"] == "Sure, sending the deck shortly."
    assert obs["features"]["body_len"] == len("Sure, sending the deck shortly.")
    assert obs["features"]["edit_delta_chars"] == 0
    assert obs["source"] == "email_draft_action"


def test_draft_edit_emits_correction_with_delta(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("core.webhook.email.emit_observation", rec)

    old_body = "Sure, sending the deck shortly."
    new_body = "Sure — sending the updated deck today, EOD."
    _run(
        _emit_draft_observation(
            "correction",
            "corrected",
            old_body,
            actual_body=new_body,
            edit_delta_chars=abs(len(new_body) - len(old_body)),
        )
    )

    obs = rec.calls[0]
    assert obs["event_type"] == "correction"
    assert obs["outcome"] == "corrected"
    # predicted = the AI draft, actual = the user's fix — the delta is the lesson
    assert obs["predicted"] == old_body
    assert obs["actual"] == new_body
    assert obs["features"]["edit_delta_chars"] == abs(len(new_body) - len(old_body))


def test_draft_drop_emits_rejected(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("core.webhook.email.emit_observation", rec)

    _run(_emit_draft_observation("rejection", "rejected", "Attached is the proposal."))

    obs = rec.calls[0]
    assert obs["event_type"] == "rejection"
    assert obs["outcome"] == "rejected"
    assert obs["predicted"] == "Attached is the proposal."
    assert obs["actual"] is None


def test_draft_bodies_truncated_to_500_chars(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("core.webhook.email.emit_observation", rec)

    long_body = "x" * 2000
    _run(_emit_draft_observation("approval", "confirmed", long_body))

    obs = rec.calls[0]
    assert len(obs["predicted"]) == 500
    assert obs["features"]["body_len"] == 2000
