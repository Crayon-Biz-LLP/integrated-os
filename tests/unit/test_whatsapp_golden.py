"""Stage D — Golden-set harness for WhatsApp classification.

Runs the deterministic stages (sieve + ask-detector) against the 19
hand-labeled real threads in tests/golden/whatsapp_classify/golden.json.
The LLM classify (Stage C) is asserted separately (it needs a live/mocked
call); the deterministic stages must be 100% on the golden set.

The golden set is the contract: a classifier change is only "done" when
this harness passes. Extend golden.json with new labeled threads as the
system sees more real conversations.
"""

import json
from pathlib import Path

import pytest

from core.lib.message_sieve import classify_sieve
from core.lib.ask_detector import should_escalate
from core.lib.chat_split import split_chat_identity

GOLDEN_PATH = Path(__file__).parent.parent / "golden" / "whatsapp_classify" / "golden.json"

# The user's first name (mention detection in Stage B)
USER_NAME = "Danny"


def _load_golden():
    with open(GOLDEN_PATH) as f:
        return json.load(f)


def test_golden_set_present_and_complete():
    cases = _load_golden()
    assert len(cases) >= 15, "golden set should cover ≥15 real threads"
    categories = {c["category"] for c in cases}
    assert categories, "golden set must be categorized"
    # every case has a target, expected classification, and stage gates
    for c in cases:
        assert c["target"], f"{c['id']}: target missing"
        assert c["expected"]["classification"] in ("actionable", "fyi", "ignored"), c["id"]
        assert "survives_sieve" in c and "escalates" in c, c["id"]


@pytest.mark.parametrize("case", _load_golden(), ids=lambda c: c["id"])
def test_sieve_matches_golden(case):
    """Stage A must agree with the golden labels on what is noise."""
    verdict = classify_sieve(
        case["target"],
        sender_name=case.get("target_participant"),
        participant=case.get("target_participant"),
    )
    expected_noise = not case["survives_sieve"]
    assert verdict["noise"] == expected_noise, (
        f"{case['id']}: sieve noise={verdict['noise']} (expected {expected_noise}, "
        f"reason={verdict['reason']}) for: {case['target'][:60]}"
    )


@pytest.mark.parametrize("case", _load_golden(), ids=lambda c: c["id"])
def test_ask_detector_matches_golden(case):
    """Stage B must escalate exactly the messages the golden set expects.

    Only meaningful for messages that survive the sieve (noise never reaches
    the ask-detector).
    """
    if not case["survives_sieve"]:
        pytest.skip("message is sieve-noise; ask-detector never sees it")

    result = should_escalate(case["target"], user_name=USER_NAME)
    assert result["escalate"] == case["escalates"], (
        f"{case['id']}: ask-detector escalate={result['escalate']} "
        f"(expected {case['escalates']}) signals={result['signals']} "
        f"for: {case['target'][:60]}"
    )


@pytest.mark.parametrize("case", _load_golden(), ids=lambda c: c["id"])
def test_chat_split_consistent(case):
    """Stage 0: splitting the RAW sender_id yields the golden chat_id.

    The raw sender_id is the phone-stamped identity (e.g. "ACC: Tech &
    Production : Binu ACC"); the golden chat_id is the extracted prefix
    ("ACC: Tech & Production"). Note group names may THEMSELVES contain
    colons ("Solvstrat: Core Team") — rsplit on the LAST colon is what
    makes this exact.
    """
    raw = case.get("raw_sender_id")
    if not raw:
        pytest.skip("no raw sender_id recorded")
    split = split_chat_identity(raw)
    assert split["chat_id"] == case["chat_id"], (
        f"{case['id']}: split('{raw}') → chat_id={split['chat_id']!r} "
        f"(expected {case['chat_id']!r})"
    )
