"""Junk / background ASR must not become a caller turn."""

from turn_filter import classify_user_turn, extract_name_correction


def test_name_correction_is_task_not_junk() -> None:
    verdict = classify_user_turn("My name is Johnson")
    assert verdict.ignore is False
    assert verdict.task is True
    assert verdict.name_correction == "Johnson"
    assert extract_name_correction("it's actually Johnson") == "Johnson"


def test_live_junk_fragments_are_ignored() -> None:
    for text in (
        "Ne güzel",
        "puedes buscar el mundo",
        "Iya",
        "Load what?",
        "Mm",
        "Mhm",
        "Yeah",
        "",
        "  ",
    ):
        verdict = classify_user_turn(
            text,
            tool_in_flight=True,
            recent_fillers=["mm, c'moooon, load"],
        )
        assert verdict.ignore is True, text


def test_in_flight_hello_is_barge_not_goal_change() -> None:
    verdict = classify_user_turn("hello", tool_in_flight=True)
    assert verdict.ignore is False
    assert verdict.barge is True
    assert verdict.task is False


def test_cancel_intent_is_never_junk() -> None:
    verdict = classify_user_turn(
        "I need to cancel my appointment and book a check-up",
        tool_in_flight=True,
    )
    assert verdict.ignore is False
    assert verdict.task is True


def test_substantial_non_english_speech_gets_an_english_reply_not_silence() -> None:
    """A non-English caller must hear something, not dead air.

    _has_non_latin_letters ignores every non-Latin turn, so a Hindi or Sindhi
    caller was dropped on every turn and heard nothing at all. Ava stays
    English-only, but she has to say so out loud.
    """
    verdict = classify_user_turn("मुझे अपॉइंटमेंट चाहिए, मेरे दांत में दर्द है")
    assert verdict.ignore is False
    assert verdict.needs_english_notice is True


def test_short_non_latin_scrap_is_still_ignored() -> None:
    """Background TV and stray syllables must not trigger the notice."""
    for scrap in ("可以吧。", "उह"):
        verdict = classify_user_turn(scrap)
        assert verdict.ignore is True, scrap
        assert verdict.needs_english_notice is False, scrap


def test_non_english_during_a_tool_call_is_still_ignored() -> None:
    """Mid-lookup chatter must not interrupt the ladder."""
    verdict = classify_user_turn(
        "मुझे अपॉइंटमेंट चाहिए, मेरे दांत में दर्द है", tool_in_flight=True
    )
    assert verdict.ignore is True
    assert verdict.needs_english_notice is False


def test_english_turns_never_ask_for_the_notice() -> None:
    for text in ("I need an appointment", "What time is it now?", "yeah nah"):
        assert classify_user_turn(text).needs_english_notice is False
