"""Grounding gate: only SpeakableFacts from successful tools may be spoken."""

import logging
from datetime import date

import pytest

from call_state import CallState
from grounding import (
    CONFIRM_SUBSTITUTE,
    DATE_SUBSTITUTE,
    GATE_PASS_DOB_UTTERANCES,
    SAFE_SUBSTITUTE,
    SpeakableFacts,
    confirm_claim_kind,
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


def test_confirm_intent_is_not_a_completed_booking() -> None:
    from grounding import (
        GATE_BLOCK_UTTERANCES,
        GATE_PASS_UTTERANCES,
        SpeakableFacts,
        gate_utterance,
    )

    facts = SpeakableFacts()
    assert len(GATE_BLOCK_UTTERANCES) == 20
    assert len(GATE_PASS_UTTERANCES) == 20
    for line in GATE_BLOCK_UTTERANCES:
        gated = gate_utterance(line, facts)
        assert gated.suppressed is True, line
        assert "confirm" in gated.violations or gated.recovery, line
        assert gated.recovery is True
        assert gated.spoken != line
    for line in GATE_PASS_UTTERANCES:
        gated = gate_utterance(line, facts)
        assert gated.suppressed is False, (line, gated.violations, gated.spoken)
        assert gated.spoken == line
    locked_in = gate_utterance("let me lock that in for ya", facts)
    assert locked_in.suppressed is False


def test_gate_allows_dob_confirm_and_privacy_callback_phrases() -> None:
    facts = SpeakableFacts()
    assert "confirm your date of birth" in GATE_PASS_DOB_UTTERANCES
    for line in GATE_PASS_DOB_UTTERANCES:
        kind = confirm_claim_kind(line)
        assert kind != "completed", line
        assert kind != "ambiguous", line
        gated = gate_utterance(line, facts)
        assert gated.suppressed is False, (line, gated.violations, gated.spoken)
        assert gated.ambiguous is False, line
        assert gated.spoken == line
        assert "you're booked" not in line.lower()

    blocked = gate_utterance("You're all set, booked in.", facts)
    assert blocked.suppressed is True
    assert blocked.recovery is True


def test_ambiguous_log_once_per_completed_utterance_not_per_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    facts = SpeakableFacts()
    spoken = "confirm this for me"
    with caplog.at_level(logging.INFO, logger="ava.grounding"):
        accumulated = ""
        for char in spoken:
            accumulated += char
            gate_utterance(accumulated, facts, log=False)
        gate_utterance(spoken, facts, log=True)
        gate_utterance(spoken + " please", facts, log=True)
        gate_utterance("lock it", facts, log=True)
    lines = [
        record.getMessage()
        for record in caplog.records
        if "GROUNDING_AMBIGUOUS" in record.getMessage()
    ]
    assert len(lines) == 2, lines
    assert "confirm this for me" in lines[0]
    assert "lock it" in lines[1]


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
async def test_realtime_transcription_yields_recovery_not_ungrounded() -> None:
    """Captions become the recovery line; original confirm text is not yielded."""
    from grounding import RECOVERY_DEFAULT

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

    assert yielded == RECOVERY_DEFAULT
    assert "Maryam" not in yielded
    assert rewrites == [RECOVERY_DEFAULT]
    assert committed[0] == "You're all set, "
