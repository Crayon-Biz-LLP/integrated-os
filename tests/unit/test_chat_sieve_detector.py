"""Unit tests for the deterministic classification stages.

- Stage 0: chat/participant split (core.lib.chat_split)
- Stage A: message sieve (core.lib.message_sieve)
- Stage B: ask-detector (core.lib.ask_detector)

These stages are pure and cost-free — they gate whether the LLM ever sees a
message. Getting them right is what collapses the FYI backlog.
"""

from core.lib.chat_split import split_chat_identity, is_automated_participant, normalize_chat_key
from core.lib.message_sieve import classify_sieve
from core.lib.ask_detector import should_escalate


# ── Stage 0: chat/participant split ──────────────────────────────────

def test_split_group_with_colon():
    r = split_chat_identity("CirroCraft - Paulsons Ledgers: Nathan")
    assert r["chat_id"] == "CirroCraft - Paulsons Ledgers"
    assert r["participant"] == "Nathan"
    assert r["is_group"] is True


def test_split_group_multiword_participant():
    r = split_chat_identity("ACC Elders + Danny: Abhishek Paul ACC")
    assert r["chat_id"] == "ACC Elders + Danny"
    assert r["participant"] == "Abhishek Paul ACC"
    assert r["is_group"] is True


def test_split_1to1_no_colon():
    r = split_chat_identity("Mohammed Yazir Crayon Employee")
    assert r["chat_id"] == "Mohammed Yazir Crayon Employee"
    assert r["participant"] is None
    assert r["is_group"] is False


def test_split_phone_number():
    r = split_chat_identity("+91 84471 49749")
    assert r["is_group"] is False
    assert r["participant"] is None
    assert r["chat_id"] == "+91 84471 49749"


def test_split_empty():
    r = split_chat_identity(None)
    assert r["chat_id"] == ""
    assert r["is_group"] is False


def test_normalize_chat_key_groups_collapse():
    a = normalize_chat_key("ACC Elders + Danny: Marcus Durai")
    b = normalize_chat_key("ACC Elders + Danny: Abhishek Paul ACC")
    assert a == b == "ACC Elders + Danny"


def test_automated_participant_detection():
    assert is_automated_participant("Mention Mirror")
    assert is_automated_participant("Translator")
    assert not is_automated_participant("Marcus Durai")
    assert not is_automated_participant(None)


# ── Stage A: sieve ───────────────────────────────────────────────────

def test_sieve_media_only():
    assert classify_sieve("Sent a picture")["noise"] is True
    assert classify_sieve("Sent a voice note")["noise"] is True
    assert classify_sieve("Sent a sticker")["noise"] is True


def test_sieve_media_with_caption_survives():
    # "Sent a picture" plus a real ask → keep (has_real_text)
    r = classify_sieve("Sent a picture --- Can you please ask the auditor to clarify")
    assert r["noise"] is False


def test_sieve_emoji_only():
    assert classify_sieve("😂")["noise"] is True
    assert classify_sieve("👍")["noise"] is True


def test_sieve_reaction_token():
    assert classify_sieve("ok")["noise"] is True
    assert classify_sieve("Oh wow looks great 😃")["noise"] is True
    assert classify_sieve("Amen")["noise"] is True


def test_sieve_automated_participant():
    r = classify_sieve("@all come for tunnel", participant="Mention Mirror")
    assert r["noise"] is True
    assert r["reason"] == "automated_participant"


def test_sieve_automated_sender():
    r = classify_sieve("Heavy rainfall warning", sender_name="WhatsApp bridge bot")
    assert r["noise"] is True
    assert r["reason"] == "automated_sender"


def test_sieve_junk_timestamps():
    assert classify_sieve("-153722867280912:-55")["noise"] is True
    assert classify_sieve("1234")["noise"] is True


def test_sieve_real_text_survives():
    assert classify_sieve("Please let me know when we could talk about 2 issues on the CNF account")["noise"] is False
    assert classify_sieve("Hi bro --- When will we start the AI gateway project?")["noise"] is False


def test_sieve_does_not_drop_commitments():
    # Terse commitments are real asks — must survive the sieve for the
    # ask-detector. Regression: "I will check" was wrongly dropped when
    # check/will/i were in _REACTION_WORDS.
    assert classify_sieve("I will check")["noise"] is False
    assert classify_sieve("will check")["noise"] is False
    assert classify_sieve("Book gas for home")["noise"] is False


# ── Stage B: ask-detector ────────────────────────────────────────────

def test_ask_detector_question_shape():
    r = should_escalate("When will we be able to return")
    assert r["escalate"] is True


def test_ask_detector_ask_phrase():
    r = should_escalate("Can you please ask the auditor to clarify the difference")
    assert r["escalate"] is True
    assert any("ask_phrase" in s for s in r["signals"])


def test_ask_detector_urgency():
    r = should_escalate("This is quite urgent, we need to talk today")
    assert r["escalate"] is True
    assert any("urgency" in s for s in r["signals"])


def test_ask_detector_name_mention():
    r = should_escalate("Danny can u remove alpha and teens alpha from the website", user_name="Danny")
    assert r["escalate"] is True
    assert "name_mention" in r["signals"]


def test_ask_detector_graph_person():
    r = should_escalate("Nathan shared the balance sheet", graph_names=["Nathan"])
    assert r["escalate"] is True


def test_ask_detector_noise_stays_down():
    # Chit-chat with no ask shape/urgency/name → no LLM call
    r = should_escalate("Yes yess", user_name="Danny")
    assert r["escalate"] is False
    r2 = should_escalate("Praying for your Mom, Kristin and Ashish. 🙏", user_name="Danny")
    assert r2["escalate"] is False


def test_ask_detector_empty():
    assert should_escalate("")["escalate"] is False
    assert should_escalate(None)["escalate"] is False


def test_ask_detector_no_false_positive_on_short_hi():
    # "Hi Danny" alone is a greeting, not an ask — but the name mention
    # escalates (it could be a prelude to a request). This is the designed
    # trade-off: cheaper to ask the LLM than to miss a real request.
    r = should_escalate("Hi Danny", user_name="Danny")
    assert r["escalate"] is True
