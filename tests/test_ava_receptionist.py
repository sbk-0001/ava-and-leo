"""Ava Realtime voice defaults, barge-in settings, and console-safe EndCallTool."""

import inspect
from types import SimpleNamespace

import pytest
from livekit.agents.beta.tools import EndCallTool

from ava_receptionist import (
    AVA_DEFAULT_VOICE,
    AVA_REALTIME_SPEED,
    AvaReceptionist,
    ava_realtime_model,
    inbound_greeting_instructions,
    resolve_ava_voice,
)
from practice import PracticeClient


def test_default_realtime_voice_is_marin() -> None:
    assert AVA_DEFAULT_VOICE == "marin"
    assert resolve_ava_voice(env={}) == "marin"
    assert resolve_ava_voice(env={"AVA_REALTIME_VOICE": ""}) == "marin"
    assert resolve_ava_voice(env={"AVA_REALTIME_VOICE": "cedar"}) == "cedar"
    assert resolve_ava_voice(env={"LEO_REALTIME_VOICE": "cedar"}) == "cedar"


def test_end_call_tool_ignores_on_enter() -> None:
    source = inspect.getsource(AvaReceptionist.__init__)
    assert "ignore_on_enter" in source
    assert "ignore_on_enter" in inspect.signature(EndCallTool.__init__).parameters


def test_inbound_greeting_is_ava_and_human() -> None:
    """First line is Ava, warm, one short sentence plus one question."""
    text = inbound_greeting_instructions("dapto")
    lowered = text.lower()
    assert "ava" in lowered
    assert "leo" not in lowered
    assert "dapto dentists" in lowered
    assert "dapto" in lowered
    assert "warm" in lowered or "human" in lowered
    assert "booking" in lowered
    assert "one" in lowered and "question" in lowered
    assert "short" in lowered
    assert len(text) < 400


def test_ava_worker_publishes_desk_feed_from_session_events() -> None:
    """Transcript still uses conversation_item_added; tools emit activity.

    Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
    """
    from agent import _register_desk_feed, my_agent

    source = inspect.getsource(_register_desk_feed) + inspect.getsource(my_agent)
    assert "conversation_item_added" in source
    assert "function_tools_executed" not in inspect.getsource(_register_desk_feed)
    assert "_register_desk_feed" in inspect.getsource(my_agent)
    assert 'persona_key == "ava"' in inspect.getsource(my_agent)
    assert "_notify_desk" in inspect.getsource(AvaReceptionist)


def test_ava_session_uses_realtime_llm_and_interruptions() -> None:
    from agent import _build_ava_session

    source = inspect.getsource(_build_ava_session)
    assert "realtime_llm" in source
    assert "enabled" in source
    assert "True" in source


def test_realtime_model_is_slower_marin(monkeypatch) -> None:
    """Unhurried playback; keep marin unless a better feminine voice is verified.

    Docs: https://docs.livekit.io/reference/python/livekit/plugins/openai/realtime/
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert AVA_REALTIME_SPEED == 0.85
    source = inspect.getsource(ava_realtime_model)
    assert "speed" in source
    assert "AVA_REALTIME_SPEED" in source
    model = ava_realtime_model()
    assert model._opts.speed == 0.85
    assert model._opts.voice == "marin"


def test_realtime_model_enables_barge_in_and_snappy_vad() -> None:
    """OpenAI Realtime turn detection must allow barge-in and close turns quickly.

    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
          https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode
    """
    source = inspect.getsource(ava_realtime_model)
    assert "interrupt_response" in source
    assert "True" in source
    assert "server_vad" in source
    assert "silence_duration_ms" in source
    assert "400" in source
    assert "create_response" in source


@pytest.mark.asyncio
async def test_practice_tools_notify_desk_on_success(monkeypatch) -> None:
    """Desk activity must fire from the tool method, not only session events."""
    practice = PracticeClient(mode="mock")
    practice.seed_slot(
        slot_id="slot-am",
        branch_id="shellharbour",
        date="2026-09-16",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    events: list[dict] = []
    ava = AvaReceptionist(
        branch_id="shellharbour",
        practice=practice,
        on_desk_event=events.append,
    )
    dummy = SimpleNamespace()

    found = await ava.find_patient(dummy, name="Jamie Cole", phone="0412222333")
    assert found["ok"] is True
    booked = await ava.book_appointment(
        dummy,
        slot_id="slot-am",
        reason="check-up",
        name="Jamie Cole",
        phone="0412222333",
    )
    assert booked["confirmed"] is True
    moved = await ava.reschedule_appointment(
        dummy, booking_id=booked["booking_id"], new_slot_id="missing"
    )
    assert moved["ok"] is False
    cancelled = await ava.cancel_appointment(dummy, booking_id=booked["booking_id"])
    assert cancelled["confirmed"] is True
    message = await ava.leave_message(
        dummy, caller_name="Sam Lee", phone="0412000000", body="Call back"
    )
    assert message["ok"] is True
    available = await ava.get_availability(dummy, date="2026-09-16")
    assert available["ok"] is True

    actions = [event["action"] for event in events]
    assert "find_patient" in actions
    assert "book_appointment" in actions
    assert "cancel_appointment" in actions
    assert "leave_message" in actions
    assert "get_availability" in actions
    assert "reschedule_appointment" not in actions
    book = next(event for event in events if event["action"] == "book_appointment")
    assert book["refresh_diary"] is True
    assert book["payload"]["name"] == "Jamie Cole"
