"""Ava Realtime voice defaults, barge-in, tools, and branch greeting."""

import inspect
from datetime import date
from types import SimpleNamespace

import pytest

from ava_receptionist import (
    AVA_DEFAULT_VOICE,
    AVA_SPEECH_SPEED,
    AVA_TEMPERATURE,
    AVA_VAD_SILENCE_MS,
    AvaReceptionist,
    ava_realtime_model,
    inbound_greeting_instructions,
    resolve_ava_voice,
    transfer_destination_for_branch,
)


def test_default_realtime_voice_is_marin() -> None:
    assert AVA_DEFAULT_VOICE == "marin"
    assert resolve_ava_voice(env={}) == "marin"
    assert resolve_ava_voice(env={"AVA_REALTIME_VOICE": ""}) == "marin"
    assert resolve_ava_voice(env={"AVA_REALTIME_VOICE": "cedar"}) == "cedar"
    assert resolve_ava_voice(env={"LEO_REALTIME_VOICE": "cedar"}) == "cedar"


def test_availability_tools_require_real_slot_ids() -> None:
    check = inspect.getsource(AvaReceptionist.check_availability)
    book = inspect.getsource(AvaReceptionist.book_appointment)
    assert "must check" in check.lower() or "before offering" in check.lower()
    assert "next week" in check
    assert "exact slot_id" in book
    assert "invalid_slot_id" in book
    assert "check_availability" in book


def test_required_tools_are_present() -> None:
    source = inspect.getsource(AvaReceptionist)
    for name in (
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "lookup_patient",
        "quote_fee",
        "take_message",
        "transfer_to_human",
        "end_call",
    ):
        assert f"async def {name}" in source, name
    assert "TransferSIPParticipantRequest" in source
    assert "DeleteRoomRequest" in source
    assert "_cover" in source
    assert "_dispatch_with_ladder" in source
    assert "generate_reply" in source
    assert "first-audio-ts" in source
    tool_src = inspect.getsource(AvaReceptionist.check_availability)
    assert "next week" in tool_src
    assert "next tuesday" in tool_src.lower() or "next <weekday>" in tool_src.lower()
    assert "clinician" in tool_src
    book = inspect.getsource(AvaReceptionist.book_appointment)
    assert "exact slot_id" in book
    assert "invalid_slot_id" in book
    assert "_notify_desk" in inspect.getsource(AvaReceptionist)


def test_inbound_greeting_is_the_mapped_branch() -> None:
    """DID maps the branch. She answers as that clinic — never a group menu."""
    text = inbound_greeting_instructions("shellharbour")
    lowered = text.lower()
    assert "ava" in lowered
    assert "shellharbour dentists" in lowered
    assert "morning, shellharbour dentists, ava speaking" in lowered
    assert "how ya going" in lowered
    assert "what can i do for ya" in lowered
    assert "never ask which clinic" in lowered
    assert "illawarra dentists group" not in lowered
    assert "list all three" not in lowered

    dapto = inbound_greeting_instructions("dapto").lower()
    assert "dapto dentists" in dapto
    assert "ava at illawarra dentists" not in dapto


def test_inbound_greeting_uses_did_branch() -> None:
    for branch_id, name in (
        ("shellharbour", "shellharbour dentists"),
        ("dapto", "dapto dentists"),
        ("woonona", "woonona dentists"),
    ):
        text = inbound_greeting_instructions(branch_id).lower()
        assert name in text
        assert "never ask which clinic" in text
        assert "how ya going" in text
        assert "what can i do for ya" in text


def test_ava_session_uses_realtime_llm_and_interruptions() -> None:
    from agent import _build_ava_session

    source = inspect.getsource(_build_ava_session)
    assert "realtime_llm" in source
    assert "enabled" in source
    assert "True" in source


def test_realtime_model_server_vad_barge_in() -> None:
    """Server VAD 450-550ms, barge-in on, speech ~0.9, temperature 0.9-1.0.

    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
    """
    source = inspect.getsource(ava_realtime_model)
    assert "interrupt_response" in source
    assert "True" in source
    assert "server_vad" in source
    assert "silence_duration_ms" in source
    assert "AVA_VAD_SILENCE_MS" in source
    assert 450 <= AVA_VAD_SILENCE_MS <= 550
    assert AVA_SPEECH_SPEED == 0.9
    assert 0.9 <= AVA_TEMPERATURE <= 1.0
    assert "create_response" in source


def test_transfer_destination_prefers_branch_env() -> None:
    dest = transfer_destination_for_branch(
        "dapto",
        env={
            "SIP_TRANSFER_DAPTO": "+61242880737",
            "SIP_TRANSFER_TO": "+61242169911",
        },
    )
    assert dest == "+61242880737"
    fallback = transfer_destination_for_branch(
        "woonona", env={"SIP_TRANSFER_TO": "+61242169911"}
    )
    assert fallback == "+61242169911"


def test_agent_builds_call_state_before_speech() -> None:
    from agent import my_agent

    source = inspect.getsource(my_agent)
    assert "CallState(branch=branch_id" in source
    assert "call_state ready before speech" in source
    assert "today=%s" in source
    assert "kill_switch" in source
    assert "inbound_greeting_instructions(call_state.branch)" in source
    assert "is_rate_limit_error" in source
    assert "RateLimitRecovery" in source
    assert "CachedBookingProvider" in source
    assert "AmbientBed" in source
    assert "attach_backchannels" in source
    assert "mark_interrupted" in source
    assert "_register_desk_feed(session, ctx.room)" in source


@pytest.mark.asyncio
async def test_practice_tools_notify_desk_including_booking_failures(
    monkeypatch,
) -> None:
    """Desk activity must fire from the tool method for web and SIP."""
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    practice = PracticeClient(mode="mock")
    practice.seed_slot(
        slot_id="slot_shellharbour_2026-09-16_0930_dr-mohit-tolani",
        branch_id="shellharbour",
        date="2026-09-16",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    events: list[dict] = []
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=MemoryBookingProvider(practice),
        on_desk_event=events.append,
    )

    async def _run_only(self, context, factory):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    dummy = SimpleNamespace()

    found = await ava.lookup_patient(dummy, mobile="0412222333")
    assert found["ok"] is True
    booked = await ava.book_appointment(
        dummy,
        slot_id="slot_shellharbour_2026-09-16_0930_dr-mohit-tolani",
        reason="check-up",
        name="Jamie Cole",
        mobile="0412222333",
    )
    assert booked["confirmed"] is True
    assert ava.state.may_confirm_booking() is True
    invented = await ava.book_appointment(
        dummy,
        slot_id="slot-8-30-tuesday-dr-mohit-tolani-follow-up",
        reason="follow-up",
        name="Bill Gates",
        mobile="0412000111",
    )
    assert invented["reason"] == "invalid_slot_id"
    assert invented["confirmed"] is False
    assert "not locked" in invented["say"].lower()
    assert ava.state.may_confirm_booking() is False
    moved = await ava.reschedule_appointment(
        dummy, booking_id=booked["booking_id"], new_slot_id="missing"
    )
    assert moved["ok"] is False
    cancelled = await ava.cancel_appointment(dummy, booking_id=booked["booking_id"])
    assert cancelled["confirmed"] is True
    message = await ava.take_message(
        dummy, name="Sam Lee", mobile="0412000000", reason="Call back"
    )
    assert message["ok"] is True
    available = await ava.check_availability(
        dummy, appointment_type="check-up", date_range="2026-09-16"
    )
    assert available["ok"] is True

    actions = [event["action"] for event in events]
    assert "lookup_patient" in actions
    assert "book_appointment" in actions
    assert "cancel_appointment" in actions
    assert "take_message" in actions
    assert "check_availability" in actions
    assert "reschedule_appointment" not in actions
    books = [event for event in events if event["action"] == "book_appointment"]
    assert any(event["refresh_diary"] for event in books)
    assert any(
        event["payload"].get("failure_reason") == "invalid_slot_id" for event in books
    )
    success = next(event for event in books if event["refresh_diary"])
    assert success["payload"]["name"] == "Jamie Cole"


@pytest.mark.asyncio
async def test_book_appointment_slot_gone_is_not_verbally_confirmed(
    monkeypatch,
) -> None:
    from call_state import CallState

    class _Gone:
        async def book_appointment(self, **kwargs):
            return {
                "ok": False,
                "confirmed": False,
                "reason": "slot_gone",
                "slot_id": kwargs["slot_id"],
                "note": "That time just went. Do not say confirmed.",
            }

        async def check_availability(self, **kwargs):
            return {"ok": True, "slots": []}

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    events: list[dict] = []
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=_Gone(),
        on_desk_event=events.append,
    )

    async def _run_only(self, context, factory):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    result = await ava.book_appointment(
        SimpleNamespace(),
        slot_id="slot_shellharbour_2026-09-15_1430_dr-mohit-tolani",
        reason="broken tooth",
        name="Robert",
        mobile="0412334556",
    )
    assert result["ok"] is False
    assert result["confirmed"] is False
    assert result["reason"] == "slot_gone"
    assert ava.state.may_confirm_booking() is False
    assert "not locked" in result["say"].lower()
    assert "do not say you're all set" in result["say"].lower()
    assert events and events[-1]["payload"]["failure_reason"] == "slot_gone"


@pytest.mark.asyncio
async def test_check_availability_uses_preferred_clinician(monkeypatch) -> None:
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient, seed_mock_diary

    practice = PracticeClient(mode="mock")
    seed_mock_diary(practice, today=date(2026, 9, 15), days=7)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = CallState(branch="shellharbour", today=date(2026, 9, 15))
    state.observe_user_text("I'd like Dr Mohit please")
    ava = AvaReceptionist(state=state, booking=MemoryBookingProvider(practice))

    async def _run_only(self, context, factory):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    result = await ava.check_availability(
        SimpleNamespace(), appointment_type="emergency", date_range="this week"
    )
    assert result["ok"] is True
    assert result["slots"]
    assert all(
        "mohit" in (slot.get("clinician") or "").lower() for slot in result["slots"]
    )
    assert ava.state.may_offer_times() is True
