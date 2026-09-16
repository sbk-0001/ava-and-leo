"""Ava Realtime voice defaults, barge-in, tools, and branch greeting."""

import asyncio
import inspect
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ava_receptionist import (
    AVA_DEFAULT_VOICE,
    AVA_SPEECH_SPEED,
    AVA_TEMPERATURE,
    AVA_TRANSCRIPTION_LANGUAGE,
    AVA_VAD_SILENCE_MS,
    AvaReceptionist,
    ava_realtime_model,
    inbound_greeting_instructions,
    resolve_ava_voice,
    transfer_destination_for_branch,
)

SYDNEY = ZoneInfo("Australia/Sydney")

# Frozen Sydney clock for the seeded diary. The mock diary opens at 08:00 on
# 2026-09-15, so 07:00 that morning keeps every seeded slot in the future.
# MemoryBookingProvider defaults now_fn to the live clock, so an unpinned
# provider drops those slots as past once real time moves on and the test
# starts failing by calendar date rather than by behaviour.
SYDNEY_NOW = datetime(2026, 9, 15, 7, 0, tzinfo=SYDNEY)


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
        "resolve_date_phrase",
        "current_time_sydney",
        "ask_for_field",
        "verify_date_of_birth",
        "correct_caller_name",
        "read_date_of_birth",
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
    assert "transcription_node" in inspect.getsource(AvaReceptionist)
    assert "tts_node" in inspect.getsource(AvaReceptionist)
    assert "resolve_date_phrase" in inspect.getsource(AvaReceptionist)


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


def test_realtime_transcription_is_pinned_to_english() -> None:
    """Ava is an English-only receptionist, so ASR must not guess the language.

    On jobs AJ_zfzKY76q5L5A and AJ_BFVvzmBdUchN the caller spoke English and the
    transcript came back in Cyrillic and then Chinese script, which the turn
    filter dropped as non_task_language, so those turns reached Ava as silence.
    Pinning the transcription language stops English audio being transcribed as
    another script.
    """
    source = inspect.getsource(ava_realtime_model)
    assert "input_audio_transcription" in source
    assert "AVA_TRANSCRIPTION_LANGUAGE" in source
    assert AVA_TRANSCRIPTION_LANGUAGE == "en"


def test_ava_answers_in_english_whatever_the_caller_speaks() -> None:
    """She may be spoken to in any language; she always replies in English."""
    from persona import VOICE_INSTRUCTIONS

    lowered = VOICE_INSTRUCTIONS.lower()
    assert "english" in lowered
    # An explicit never-switch rule, not just the word "English" in passing.
    assert "another language" in lowered or "other language" in lowered
    assert "australian english" in lowered


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
    assert "CallState(" in source
    assert "branch=branch_id" in source
    assert "call_state ready before speech" in source
    assert "today=%s" in source
    assert "kill_switch" in source
    assert "greet_on_enter" in source
    assert "assert_sip_host" in source
    assert "assert_live_openai_key" in source
    assert "RateLimitCircuitBreaker" in source
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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    # Freeze the Sydney clock. MemoryBookingProvider defaults now_fn to the live
    # clock, so an unfrozen provider drops the seeded 09:30 slot as "past" once
    # real Sydney time passes it, and the booking below stops confirming.
    now = datetime(2026, 9, 15, 9, 0, tzinfo=ZoneInfo("Australia/Sydney"))
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
        state=CallState(branch="shellharbour", now=now),
        booking=MemoryBookingProvider(practice, now_fn=lambda: now),
        on_desk_event=events.append,
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    dummy = SimpleNamespace()

    found = await ava.lookup_patient(dummy, mobile="0412222333")
    assert found["ok"] is True
    available = await ava.check_availability(
        dummy, appointment_type="check-up", date_range="2026-09-16"
    )
    assert available["ok"] is True
    assert available["status"] == "OK"
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
    ava.state.dob_verified = True
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

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    ava.state.booking_flow.offer_slots(
        [{"slot_id": "slot_shellharbour_2026-09-15_1430_dr-mohit-tolani"}]
    )
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


class _PlayedFiller:
    def __init__(self) -> None:
        self.played: list[str] = []
        self.last_first_audio_ts = 0.0
        self.session = None
        self.ambient = None
        self.on_audio = None

    async def play(self, text: str, **_kwargs: object) -> None:
        self.played.append(text)


@pytest.mark.asyncio
async def test_book_without_name_asks_and_does_not_call_practice(
    monkeypatch,
) -> None:
    from call_state import CallState

    class _Spy:
        def __init__(self) -> None:
            self.calls = 0

        async def book_appointment(self, **kwargs):
            self.calls += 1
            raise AssertionError(f"practice book should not run: {kwargs}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    player = _PlayedFiller()
    spy = _Spy()
    ava = AvaReceptionist(
        state=CallState(
            branch="shellharbour",
            today=date(2026, 9, 15),
            caller_mobile="0412334556",
        ),
        booking=spy,
        filler_player=player,
    )
    ava.state.booking_flow.offer_slots(
        [{"slot_id": "slot_shellharbour_2026-09-16_0930_dr-mohit-tolani"}]
    )
    result = await ava.book_appointment(
        SimpleNamespace(),
        slot_id="slot_shellharbour_2026-09-16_0930_dr-mohit-tolani",
        reason="check-up",
        name=None,
        mobile="0412334556",
    )
    assert result["ok"] is False
    assert result.get("confirmed") is not True
    assert result["reason"] == "need_fields"
    assert "name" in result["need_fields"]
    assert "name" in result["say"].lower()
    assert spy.calls == 0
    assert player.played
    assert ava.state.may_confirm_booking() is False


@pytest.mark.asyncio
async def test_verify_dob_fail_speaks_immediately_and_retries_once(
    monkeypatch,
) -> None:
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    player = _PlayedFiller()
    client = PracticeClient(mode="mock")
    client.seed_patient(
        patient_id="pat_sam",
        name="Sam Smith",
        phone="0449004305",
        date_of_birth="1989-08-23",
    )
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=MemoryBookingProvider(client, now_fn=lambda: SYDNEY_NOW),
        filler_player=player,
    )
    ava.state.pms_record = {
        "ok": True,
        "patients": [
            {
                "patient_id": "pat_sam",
                "name": "Sam Smith",
                "date_of_birth": "1989-08-23",
            }
        ],
    }
    first = await ava.verify_date_of_birth(SimpleNamespace(), "1980-01-01")
    assert first["ok"] is False
    assert first["retry_allowed"] is True
    assert player.played
    second = await ava.verify_date_of_birth(SimpleNamespace(), "23rd August 1989")
    assert second["ok"] is True
    assert ava.state.dob_verified is True
    assert client.patients["pat_sam"].date_of_birth == "1989-08-23"


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
    ava = AvaReceptionist(
        state=state, booking=MemoryBookingProvider(practice, now_fn=lambda: SYDNEY_NOW)
    )

    async def _run_only(self, context, factory, **_kwargs):
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
    assert result["status"] == "OK"


@pytest.mark.asyncio
async def test_check_availability_omits_past_morning_slots_at_sydney_1730(
    monkeypatch,
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient, seed_mock_diary

    now = datetime(2026, 9, 15, 17, 30, tzinfo=ZoneInfo("Australia/Sydney"))
    practice = PracticeClient(mode="mock")
    seed_mock_diary(practice, today=date(2026, 9, 15), days=7)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", now=now),
        booking=MemoryBookingProvider(practice, now_fn=lambda: now),
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    result = await ava.check_availability(
        SimpleNamespace(), appointment_type="check-up", date_range="today"
    )
    times = [slot["time"] for slot in result.get("slots") or []]
    assert "08:00" not in times
    assert "09:30" not in times
    offer = str(result.get("offer") or "")
    assert "08:00" not in offer
    assert "09:30" not in offer
    remembered = " ".join(
        str(slot.get("time") or "") for slot in ava.state.last_availability_slots
    )
    assert "08:00" not in remembered
    assert "09:30" not in remembered
    clock = await ava.current_time_sydney(SimpleNamespace())
    assert clock["period"] == "evening"
    assert "5:30 pm" in clock["clock"]


@pytest.mark.asyncio
async def test_empty_tool_args_are_rejected(monkeypatch) -> None:
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    empty = await ava.check_availability(
        SimpleNamespace(), appointment_type="  ", date_range=""
    )
    assert empty["reason"] == "empty_tool_args"
    dated = await ava.resolve_date_phrase(SimpleNamespace(), phrase="   ")
    assert dated["reason"] == "empty_tool_args"


@pytest.mark.asyncio
async def test_resolve_date_phrase_tool_and_end_call_block(monkeypatch) -> None:
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 14)),
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
    )
    hit = await ava.resolve_date_phrase(SimpleNamespace(), phrase="next Friday")
    assert hit["ambiguous"] is True
    assert ava.state.speakable.date_resolved is False

    ava.state.booking_flow.offer_slots(
        [{"slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"}]
    )
    ava.state.booking_flow.select_slot(
        "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"
    )
    blocked = await ava.end_call(SimpleNamespace(), reason="done")
    assert blocked["reason"] == "booking_in_progress"


def test_on_enter_greets_web_and_sip() -> None:
    source = inspect.getsource(AvaReceptionist.on_enter)
    assert "inbound_greeting_instructions" in source
    assert "greet_on_enter" in source
    assert "EMERGENCY_000_SCRIPT" in inspect.getsource(AvaReceptionist)
    assert "speak_scripted" in inspect.getsource(AvaReceptionist.on_user_turn_completed)
    node = inspect.getsource(AvaReceptionist.transcription_node)
    assert "grounded_realtime_transcription" in node
    assert "_on_ungrounded_rewrite" in node


REALTIME_SAY_ERROR = (
    "trying to generate speech from text without a TTS model or a "
    "RealtimeSession that supports say(); add a TTS model to AgentSession to enable say()"
)


class _FakeRealtimeSession:
    """OpenAI Realtime: say() raises the production RuntimeError."""

    def __init__(self) -> None:
        self.tts = None
        self.llm = SimpleNamespace(capabilities=SimpleNamespace(supports_say=False))
        self.say_calls: list[object] = []
        self.replies: list[dict] = []
        self.interrupts = 0

    def say(self, *args: object, **kwargs: object) -> None:
        self.say_calls.append({"args": args, "kwargs": kwargs})
        raise RuntimeError(REALTIME_SAY_ERROR)

    def interrupt(self) -> None:
        self.interrupts += 1

    def generate_reply(self, **kwargs: object) -> SimpleNamespace:
        self.replies.append(dict(kwargs))
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_transcription_node_plays_recovery_from_bank(
    monkeypatch,
) -> None:
    """Ungrounded audio is interrupted; recovery is a bank clip, not generate_reply."""
    from livekit.agents import ModelSettings

    from booking import MemoryBookingProvider
    from call_state import CallState
    from filler_player import FillerPlayer
    from grounding import RECOVERY_DEFAULT
    from practice import PracticeClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class Player(FillerPlayer):
        def __init__(self) -> None:
            self.played: list[str] = []
            self.last_first_audio_ts = 0.0
            self.session = None
            self.ambient = None
            self.on_audio = None

        async def play(self, text: str, **_kwargs: object) -> None:
            self.played.append(text)

    player = Player()
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
        filler_player=player,
    )
    session = _FakeRealtimeSession()
    ava._speech_session = session

    async def _ungrounded():
        yield "That's confirmed — you're booked with Dr Maryam Kalo."

    yielded: list[str] = []
    async for chunk in ava.transcription_node(_ungrounded(), ModelSettings()):
        yielded.append(chunk)
    if ava._speech_tasks:
        await asyncio.gather(*ava._speech_tasks)

    assert yielded == [RECOVERY_DEFAULT]
    assert session.interrupts == 1
    assert session.replies == []
    assert player.played
    assert "Maryam" not in "".join(yielded)
    assert "you're booked" not in "".join(yielded).lower()
    assert session.say_calls == []


@pytest.mark.asyncio
async def test_emergency_script_uses_generate_reply_on_realtime(monkeypatch) -> None:
    from booking import MemoryBookingProvider
    from call_state import CallState
    from practice import PracticeClient

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour", today=date(2026, 9, 15)),
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
    )
    session = _FakeRealtimeSession()
    ava._speech_session = session
    ava.state.urgency_level = "emergency_000"

    from ava_receptionist import EMERGENCY_000_SCRIPT
    from filler_ladder import speak_scripted

    await speak_scripted(session, EMERGENCY_000_SCRIPT, kind="script")
    assert session.replies
    assert EMERGENCY_000_SCRIPT in str(session.replies[0].get("instructions") or "")
    assert session.say_calls == []
