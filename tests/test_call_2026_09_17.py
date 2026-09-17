"""Robert's desk call, 17 Sep 2026 08:10 (room ava-portal-shellharbour-433c1ea7).

  08:11:29 Ava   "There's an 8:30 with Dr Pat Pandey, or a 9:00 ..."   (19 min away)
  08:11:40 Ava   "I've got a 10:00 with Dr Mohit Tolani today. How does that sound?"
  08:11:43 Rob   "Well..."
  08:11:53 Ava   "All good. So that's Thursday the 17th, 10:00 ..."     (took it as yes)
  08:12:38 Ava   "... you're all set, Robert ..."                         (confirm #1)
  08:12:41 Ava   "Beautiful, you're all set, Robert ..."                  (confirm #2)
  08:12:55 Ava   "... You're all set for today, Thursday the 17th ..."   (confirm #3)

And every test run posted fake bookings to the live desk.
"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from booking import filter_past_slots
from call_state import CallState, is_acceptance, is_hesitation

SYDNEY = ZoneInfo("Australia/Sydney")
TEN_OFFER = "Nah yeah, we can do that. I've got a 10:00 with Dr Mohit Tolani today. How does that sound?"


# --- the test suite stays offline ---------------------------------------------


def test_tests_never_reach_the_live_desk_database_or_sms() -> None:
    import portal  # noqa: F401  (imports load_dotenv(".env.local"))
    from desk_events import desk_events_url

    for name in ("DATABASE_URL", "SMS_PROVIDER", "BREVO_API_KEY", "DESK_EVENTS_TOKEN"):
        assert os.environ.get(name, "") == "", name
    assert ":8787" not in desk_events_url()
    assert "trycloudflare" not in desk_events_url()


# --- "Well..." is not a yes -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Well...",
        "um",
        "Umm...",
        "Hmm, let me think.",
        "I'm not sure",
        "uh, hang on",
        "Er.",
    ],
)
def test_hesitations(text: str) -> None:
    assert is_hesitation(text)


@pytest.mark.parametrize(
    "text",
    [
        "Well, yes that's fine",
        "Robert.",
        "0474 470 332",
        "Yep.",
        "What time is it now?",
    ],
)
def test_not_hesitations(text: str) -> None:
    assert not is_hesitation(text)


@pytest.mark.parametrize(
    "text", ["Yes", "Yep.", "Yeah, that works", "Sounds good", "Perfect", "Ok sure"]
)
def test_acceptances(text: str) -> None:
    assert is_acceptance(text)


def test_hesitating_at_an_offer_is_remembered_until_they_say_yes() -> None:
    state = CallState(branch="shellharbour")
    state.observe_assistant_text(TEN_OFFER)
    state.observe_user_text("Well...")
    assert state.slot_hesitated is True
    block = state.prompt_block()
    assert "did not say yes" in block

    # Answering other questions is not agreeing to the time.
    state.observe_assistant_text("What name should I put it under?")
    state.observe_user_text("Robert.")
    state.observe_assistant_text(
        "I've got your mobile as zero four seven four - is that right?"
    )
    state.observe_user_text("Yep.")
    assert state.slot_hesitated is True

    state.observe_assistant_text(
        "Before I lock it in, does 10 o'clock with Dr Tolani suit you?"
    )
    state.observe_user_text("Yeah, that works.")
    assert state.slot_hesitated is False
    assert "did not say yes" not in state.prompt_block()


def _ava(monkeypatch):
    from ava_receptionist import AvaReceptionist
    from booking import MemoryBookingProvider
    from caller_store import CallerStore
    from practice import PracticeClient

    practice = PracticeClient(mode="mock")
    practice.seed_slot(
        slot_id="slot_shellharbour_2026-09-17_1000_dr-mohit-tolani",
        branch_id="shellharbour",
        date="2026-09-17",
        time="10:00",
        clinician="Dr Mohit Tolani",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = CallState(
        branch="shellharbour", now=datetime(2026, 9, 17, 8, 11, tzinfo=SYDNEY)
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
    return ava, practice


async def test_booking_waits_for_a_yes_after_a_hesitation(monkeypatch) -> None:
    ava, practice = _ava(monkeypatch)
    ctx = SimpleNamespace()
    ava.state.observe_user_text("I want to book, I broke my tooth")
    await ava.check_availability(
        ctx, appointment_type="emergency", date_range="2026-09-17"
    )
    ava.state.observe_assistant_text(TEN_OFFER)
    ava.state.observe_user_text("Well...")
    ava.state.register_mobile("0474 470 332")
    ava.state.confirm_mobile(correct=True)

    held = await ava.book_appointment(
        ctx,
        slot_id="slot_shellharbour_2026-09-17_1000_dr-mohit-tolani",
        reason="broken tooth",
        name="Robert",
    )
    assert held["ok"] is False
    assert held["reason"] == "time_not_agreed"
    assert "suit" in held["say"]
    assert not practice.bookings

    ava.state.observe_assistant_text(held["say"])
    ava.state.observe_user_text("Yes please")
    booked = await ava.book_appointment(
        ctx,
        slot_id="slot_shellharbour_2026-09-17_1000_dr-mohit-tolani",
        reason="broken tooth",
        name="Robert",
    )
    assert booked["confirmed"] is True


# --- the confirmation is said once ----------------------------------------------


class _Event:
    def __init__(self, *names: str) -> None:
        self.function_calls = [SimpleNamespace(name=n, call_id=f"c_{n}") for n in names]
        self.cancelled = False

    def cancel_tool_reply(self) -> None:
        self.cancelled = True


class _Session:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.replies: list[dict] = []

    def on(self, name, handler=None):
        def register(fn):
            self.handlers.setdefault(name, []).append(fn)
            return fn

        return register(handler) if handler else register

    def generate_reply(self, **kwargs):
        self.replies.append(kwargs)
        return SimpleNamespace()


async def test_kicked_confirmation_cancels_the_second_tool_reply(monkeypatch) -> None:
    from ava_receptionist import attach_tool_reply_guard

    ava, _ = _ava(monkeypatch)
    session = _Session()
    attach_tool_reply_guard(session, ava)
    ctx = SimpleNamespace(session=session)
    ava.state.observe_user_text("I want to book")
    await ava.check_availability(
        ctx, appointment_type="check up", date_range="2026-09-17"
    )
    ava.state.observe_assistant_text(TEN_OFFER)
    ava.state.observe_user_text("Yep")
    ava.state.register_mobile("0474 470 332")
    ava.state.confirm_mobile(correct=True)
    booked = await ava.book_appointment(
        ctx,
        slot_id="slot_shellharbour_2026-09-17_1000_dr-mohit-tolani",
        reason="broken tooth",
        name="Robert",
    )
    assert booked["confirmed"] is True
    assert len(session.replies) == 1, "one confirmation, from code"

    (handler,) = session.handlers["function_tools_executed"]
    ev = _Event("book_appointment")
    handler(ev)
    assert ev.cancelled is True, "the model must not confirm a second time"

    other = _Event("check_availability")
    handler(other)
    assert other.cancelled is False

    again = _Event("book_appointment")
    handler(again)
    assert again.cancelled is False, "the guard is one-shot"


def test_after_confirming_aloud_she_does_not_repeat_it() -> None:
    state = CallState(branch="shellharbour")
    assert "do not repeat the booking" not in state.prompt_block().lower()
    state.note_booking_confirmed_aloud()
    block = state.prompt_block().lower()
    assert "already told them the booking" in block
    assert "do not repeat the booking" in block


# --- no appointment that starts in a few minutes ------------------------------------


def test_slots_starting_within_half_an_hour_are_not_offered() -> None:
    now = datetime(2026, 9, 17, 8, 11, tzinfo=SYDNEY)
    slots = [
        {"slot_id": "a", "date": "2026-09-17", "time": "08:30"},
        {"slot_id": "b", "date": "2026-09-17", "time": "08:45"},
        {"slot_id": "c", "date": "2026-09-17", "time": "09:00"},
    ]
    kept = [s["slot_id"] for s in filter_past_slots(slots, now=now)]
    assert kept == ["b", "c"]


@pytest.mark.parametrize("text", ["Yep.", "Yeah", "Nah", "Okay", "yup"])
def test_a_bare_yes_or_no_is_an_answer_when_nothing_is_running(text: str) -> None:
    """Robert's "Yep." to "is that right?" was thrown away as filler."""
    from turn_filter import classify_user_turn

    assert classify_user_turn(text, tool_in_flight=False).ignore is False
    assert classify_user_turn(text, tool_in_flight=True).ignore is True
    assert classify_user_turn("mm", tool_in_flight=False).ignore is True


# --- every phone call is kept, even when the desk is offline ----------------------


async def test_worker_writes_transcript_lines_straight_to_the_shared_store(
    monkeypatch,
) -> None:
    """The last phone call left no transcript: lines only reached the store
    through the desk on the Mac, and the Mac's desk was down."""
    import desk_events
    from state_store import MemoryStateStore

    store = MemoryStateStore()
    monkeypatch.setattr(desk_events, "state_store_from_env", lambda: store)
    posted: list[dict] = []

    async def fake_http(packet):
        posted.append(packet)

    monkeypatch.setattr(desk_events, "post_desk_event_http", fake_http)
    room = SimpleNamespace(name="call-+61474470332", local_participant=None)
    await desk_events.emit_desk_event(
        {"type": "transcript", "role": "user", "text": "Hi, I need a check-up"},
        room=room,
    )
    rows = await store.desk_events_after(0)
    assert [p["text"] for _, p in rows] == ["Hi, I need a check-up"]
    assert rows[0][1]["room"] == "call-+61474470332"
    assert rows[0][1]["channel"] == "sip"
    assert posted == [], "the desk reads the store; posting too would duplicate"


async def test_without_a_store_the_desk_still_gets_the_post(monkeypatch) -> None:
    import desk_events

    monkeypatch.setattr(desk_events, "state_store_from_env", lambda: None)
    posted: list[dict] = []

    async def fake_http(packet):
        posted.append(packet)

    monkeypatch.setattr(desk_events, "post_desk_event_http", fake_http)
    await desk_events.emit_desk_event({"type": "transcript", "text": "hello"})
    assert len(posted) == 1


async def test_store_failure_falls_back_to_the_desk(monkeypatch) -> None:
    import desk_events

    class Broken:
        async def append_desk_event(self, packet):
            raise OSError("db down")

    monkeypatch.setattr(desk_events, "state_store_from_env", lambda: Broken())
    posted: list[dict] = []

    async def fake_http(packet):
        posted.append(packet)

    monkeypatch.setattr(desk_events, "post_desk_event_http", fake_http)
    await desk_events.emit_desk_event({"type": "transcript", "text": "hello"})
    assert len(posted) == 1


# --- 17 Sep 09:56 typed test: the mobile was read back twice ------------------------


def test_yes_to_ava_s_own_read_back_confirms_the_number_once() -> None:
    """She read "0412 000 111" back before storing it; the yes then found no
    number, so the tool stored it and made her read it back a second time."""
    state = CallState(branch="woonona")
    result = state.confirm_mobile(correct=True, mobile="0412 000 111")
    assert result["ok"] is True
    assert result["confirmed"] is True
    assert state.mobile_confirmed is True
    assert state.caller_mobile == "0412000111"
    assert "needs_confirmation" not in result


def test_yes_without_any_number_still_asks_for_it() -> None:
    state = CallState(branch="woonona")
    result = state.confirm_mobile(correct=True)
    assert result["ok"] is False
    assert "mobile" in result["note"]


def test_a_different_number_with_yes_is_not_silently_swapped() -> None:
    state = CallState(branch="woonona")
    state.register_mobile("0474 470 332")
    result = state.confirm_mobile(correct=True, mobile="0412 000 111")
    assert result["ok"] is False
    assert result["reason"] == "mobile_mismatch"
    assert state.mobile_confirmed is False
