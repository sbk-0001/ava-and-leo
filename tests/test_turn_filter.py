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
