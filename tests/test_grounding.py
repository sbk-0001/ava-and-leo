"""Grounding gate: only SpeakableFacts from successful tools may be spoken."""

from datetime import date

import pytest

from call_state import CallState
from grounding import (
    CONFIRM_SUBSTITUTE,
    DATE_SUBSTITUTE,
    SAFE_SUBSTITUTE,
    SpeakableFacts,
    gate_utterance,
    grounded_realtime_transcription,
    ingest_availability,
    ingest_book_result,
)


def test_ungrounded_time_is_suppressed() -> None:
    facts = SpeakableFacts()
    gated = gate_utterance(
        "I've got half past two with Dr Mohit.",
        facts,
    )
    assert gated.suppressed is True
    assert "GROUNDING_VIOLATION" in gated.log_line
    assert "time" in gated.violations
    assert gated.spoken == SAFE_SUBSTITUTE
    assert "half past two" not in gated.spoken.lower()


def test_slot_times_and_dentist_are_speakable() -> None:
    facts = SpeakableFacts()
    ingest_availability(
        facts,
        {
            "ok": True,
            "status": "OK",
            "slots": [
                {
                    "date": "2026-09-16",
                    "time": "11:10",
                    "clinician": "Dr Mohit Tolani",
                    "slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani",
                }
            ],
        },
        today=date(2026, 9, 15),
    )
    gated = gate_utterance(
        "I've got a ten past eleven on Wednesday with Dr Mohit Tolani. Which suits?",
        facts,
    )
    assert gated.suppressed is False
    assert gated.spoken.startswith("I've got a ten past eleven")
    assert facts.dentist_display_name == "Dr Mohit Tolani"


def test_confirm_language_blocked_until_book_confirmed() -> None:
    facts = SpeakableFacts()
    gated = gate_utterance("Beautiful, you're all set for Wednesday.", facts)
    assert gated.suppressed is True
    assert "confirm" in gated.violations
    assert gated.spoken == CONFIRM_SUBSTITUTE

    ingest_book_result(
        facts,
        {
            "ok": True,
            "confirmed": True,
            "clinician": "Dr Mohit Tolani",
            "date": "2026-09-16",
            "time": "11:10",
        },
        today=date(2026, 9, 15),
    )
    locked = gate_utterance(
        "Beautiful, you're all set — Wednesday the sixteenth, ten past eleven, "
        "with Dr Mohit Tolani.",
        facts,
    )
    assert locked.suppressed is False


def test_unresolved_date_is_a_violation() -> None:
    facts = SpeakableFacts(date_resolved=False)
    gated = gate_utterance("Next Tuesday then, I've got a two thirty.", facts)
    assert gated.suppressed is True
    assert "date" in gated.violations
    assert gated.spoken in {DATE_SUBSTITUTE, SAFE_SUBSTITUTE}


def test_chockers_only_when_ok_and_empty() -> None:
    unknown = SpeakableFacts(availability_status="UNKNOWN", slots_empty=True)
    gated = gate_utterance("Yeah nah, this week's chockers.", unknown)
    assert gated.suppressed is True
    assert "chockers" in gated.violations
    assert "chockers" not in gated.spoken.lower()

    ok_empty = SpeakableFacts(availability_status="OK", slots_empty=True)
    allowed = gate_utterance("Yeah nah, this week's chockers unfortunately.", ok_empty)
    assert allowed.suppressed is False


def test_failed_tool_does_not_ingest_facts() -> None:
    facts = SpeakableFacts()
    ingest_availability(
        facts,
        {"ok": False, "status": "UNKNOWN", "reason": "timeout", "slots": []},
        today=date(2026, 9, 15),
    )
    assert facts.availability_status == "UNKNOWN"
    assert not facts.times
    gated = gate_utterance("I've got a three forty-five this arvo.", facts)
    assert gated.suppressed is True


def test_call_state_counts_violations() -> None:
    state = CallState(branch="shellharbour", today=date(2026, 9, 15))
    result = state.gate_speech("I've got half past two with Dr Pat.")
    assert result.suppressed is True
    assert state.grounding_violations == 1
    assert "GROUNDING_VIOLATION" in result.log_line
    again = state.gate_speech("You're all set, booked in.")
    assert again.suppressed is True
    assert state.grounding_violations == 2


def test_dentist_display_name_is_pinned_from_slot() -> None:
    state = CallState(branch="shellharbour", today=date(2026, 9, 15))
    state.remember_availability(
        {
            "ok": True,
            "status": "OK",
            "slots": [
                {
                    "date": "2026-09-16",
                    "time": "11:10",
                    "clinician": "Dr Mohit Tolani",
                    "slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani",
                }
            ],
        }
    )
    assert state.speakable.dentist_display_name == "Dr Mohit Tolani"
    swapped = gate_utterance(
        "That's with Dr Pat at ten past eleven on Wednesday.",
        state.speakable,
    )
    assert "Dr Pat" not in swapped.spoken
    assert "Dr Mohit Tolani" in swapped.spoken


@pytest.mark.asyncio
async def test_realtime_transcription_yields_substitute_not_ungrounded() -> None:
    """Captions become the safe line; original confirm/time text is not yielded."""
    facts = SpeakableFacts()
    committed: list[str] = []
    rewrites: list[str] = []

    async def _chunks():
        yield "You're all set, "
        yield "you're booked with Dr Maryam."

    async for chunk in grounded_realtime_transcription(
        _chunks(),
        facts=facts,
        commit=lambda text: (
            committed.append(text) or gate_utterance(text, facts).spoken
        ),
        on_rewrite=rewrites.append,
    ):
        yielded = chunk

    assert yielded == CONFIRM_SUBSTITUTE
    assert "Maryam" not in yielded
    assert rewrites == [CONFIRM_SUBSTITUTE]
    assert committed[0] == "You're all set, "
