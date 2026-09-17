"""Illawarra Dentists is the practice; the caller picks the clinic first.

17 Sep 11:06 phone call: Robert said "I broke my tooth" and Ava went straight to
"We'll find you a spot at Shellharbour" - the clinic behind the number he rang.
The owner's rule: answer as Illawarra Dentists, ask which of the three clinics
suits (or work it out from what the caller says), then book.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from call_state import CallState, clinic_for_text
from persona import INSTRUCTIONS_PATH, format_clinic_facts

SYDNEY = ZoneInfo("Australia/Sydney")


# --- the prompt ---------------------------------------------------------------------


def test_prompt_asks_for_the_clinic_before_the_diary() -> None:
    text = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    assert "Which of our clinics suits you best" in text
    assert "before you check the diary" in text.lower()
    assert "Barrack Heights, Dapto or Woonona" in text
    assert "start there, it's usually right" not in text
    assert "Don't read out a list of clinics" not in text
    lower = format_clinic_facts("shellharbour").lower()
    assert "ask which clinic" in lower
    assert "start with the clinic above" not in lower


# --- working out the clinic from what they say ------------------------------------------


@pytest.mark.parametrize(
    ("text", "clinic"),
    [
        ("Dapto please", "dapto"),
        ("the one at Barrack Heights", "shellharbour"),
        ("Shellharbour is fine", "shellharbour"),
        ("Woonona", "woonona"),
        ("I live in Figtree", "dapto"),
        ("I'm up in Thirroul", "woonona"),
        ("Can I see Dr Chin Valsan?", "woonona"),
        ("I'd like Dr Amy Min", "dapto"),
        ("I want Mohit Tolani", None),  # works at two clinics: ask
        ("I broke my tooth", None),
        ("", None),
    ],
)
def test_clinic_for_text(text: str, clinic: str | None) -> None:
    assert clinic_for_text(text) == clinic


def test_naming_a_clinic_chooses_it() -> None:
    state = CallState(branch="shellharbour", require_branch_choice=True)
    assert state.branch_chosen is False
    assert "not chosen yet" in state.prompt_block()
    state.observe_user_text("Dapto please")
    assert state.branch_chosen is True
    assert state.branch == "dapto"
    assert "not chosen yet" not in state.prompt_block()


def test_yes_to_same_clinic_as_last_time_chooses_it() -> None:
    state = CallState(branch="dapto", require_branch_choice=True)
    state.observe_assistant_text("Barrack Heights again, or somewhere else this time?")
    state.observe_user_text("Yep, same as last time")
    assert state.branch_chosen is True
    assert state.branch == "shellharbour"


def test_yes_to_a_three_clinic_question_chooses_nothing() -> None:
    state = CallState(branch="dapto", require_branch_choice=True)
    state.observe_assistant_text(
        "Which of our clinics suits you best - Barrack Heights, Dapto or Woonona?"
    )
    state.observe_user_text("Yeah sure")
    assert state.branch_chosen is False


# --- the diary waits for the choice ---------------------------------------------------


def _ava(monkeypatch, *, require: bool = True):
    from ava_receptionist import AvaReceptionist
    from booking import MemoryBookingProvider
    from caller_store import CallerStore
    from practice import PracticeClient

    practice = PracticeClient(mode="mock")
    for branch, dentist in (
        ("shellharbour", "dr-mohit-tolani"),
        ("woonona", "dr-chin-valsan"),
    ):
        practice.seed_slot(
            slot_id=f"slot_{branch}_2026-09-18_0800_{dentist}",
            branch_id=branch,
            date="2026-09-18",
            time="08:00",
            clinician=dentist.replace("-", " ").title().replace("Dr ", "Dr "),
        )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = CallState(
        branch="shellharbour",
        now=datetime(2026, 9, 17, 11, 7, tzinfo=SYDNEY),
        require_branch_choice=require,
    )
    ava = AvaReceptionist(
        state=state,
        booking=MemoryBookingProvider(practice, now_fn=lambda: state.now),
        caller_store=CallerStore(),
        sms=None,
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    return ava


async def test_replay_1106_call_asks_for_the_clinic_before_offering_times(
    monkeypatch,
) -> None:
    ava = _ava(monkeypatch)
    ctx = SimpleNamespace()
    ava.state.observe_user_text("I was thinking about booking an appointment.")
    ava.state.observe_user_text("I broke my tooth.")
    asked = await ava.check_availability(
        ctx, appointment_type="emergency", date_range="tomorrow"
    )
    assert asked["ok"] is False
    assert asked["reason"] == "ask_branch"
    for name in ("Barrack Heights", "Dapto", "Woonona"):
        assert name in asked["say"]
    assert "slots" not in asked
    # She had just said "let's see what we've got" and covered with
    # "Scrolling, scrolling..." / "it's being a bit stubborn".
    assert "not a problem" in asked["note"]
    assert "never say you are checking" in asked["note"].lower()

    ava.state.observe_assistant_text(asked["say"])
    ava.state.observe_user_text("Woonona please")
    offered = await ava.check_availability(
        ctx, appointment_type="emergency", date_range="tomorrow"
    )
    assert offered["ok"] is True
    assert offered["branch_id"] == "woonona"
    assert all(s["branch_id"] == "woonona" for s in offered["slots"])


async def test_model_naming_a_clinic_is_not_the_caller_choosing(monkeypatch) -> None:
    ava = _ava(monkeypatch)
    ava.state.observe_user_text("I want to book a check-up")
    asked = await ava.check_availability(
        SimpleNamespace(),
        appointment_type="check-up",
        date_range="tomorrow",
        branch="shellharbour",
    )
    assert asked["reason"] == "ask_branch"


async def test_the_question_is_not_asked_forever(monkeypatch) -> None:
    ava = _ava(monkeypatch)
    ava.state.observe_user_text("I want to book a check-up")
    ctx = SimpleNamespace()
    for _ in range(2):
        asked = await ava.check_availability(
            ctx, appointment_type="check-up", date_range="tomorrow"
        )
        assert asked["reason"] == "ask_branch"
    third = await ava.check_availability(
        ctx, appointment_type="check-up", date_range="tomorrow"
    )
    assert third["ok"] is True


async def test_moving_or_cancelling_does_not_ask_for_a_clinic(monkeypatch) -> None:
    ava = _ava(monkeypatch)
    ava.state.observe_user_text("I need to reschedule my appointment")
    result = await ava.check_availability(
        SimpleNamespace(), appointment_type="existing", date_range="tomorrow"
    )
    assert result.get("reason") != "ask_branch"


async def test_desk_and_tests_without_the_rule_are_unchanged(monkeypatch) -> None:
    ava = _ava(monkeypatch, require=False)
    ava.state.observe_user_text("I want to book a check-up")
    result = await ava.check_availability(
        SimpleNamespace(), appointment_type="check-up", date_range="tomorrow"
    )
    assert result["ok"] is True
