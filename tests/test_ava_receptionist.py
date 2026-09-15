"""Ava Realtime voice defaults, barge-in settings, and console-safe EndCallTool."""

import inspect

from livekit.agents.beta.tools import EndCallTool

from ava_receptionist import (
    AVA_DEFAULT_VOICE,
    AvaReceptionist,
    ava_realtime_model,
    inbound_greeting_instructions,
    resolve_ava_voice,
)
from persona import GROUP_NAME


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


def test_booking_tools_still_present() -> None:
    """Routing change must not drop book / reschedule / cancel."""
    source = inspect.getsource(AvaReceptionist)
    assert "async def book_appointment" in source
    assert "async def reschedule_appointment" in source
    assert "async def cancel_appointment" in source
    assert "async def get_availability" in source
    assert "clinic the caller chose" in source


def test_inbound_greeting_is_ava_and_human() -> None:
    """First line is Ava at Illawarra Dentists, then clinic choice."""
    text = inbound_greeting_instructions("dapto")
    lowered = text.lower()
    assert "ava" in lowered
    assert "leo" not in lowered
    assert GROUP_NAME.lower() in lowered
    assert "ava at illawarra dentists" in lowered
    leftover = text.replace(GROUP_NAME, "")
    assert "Illawarra Dental" not in leftover
    assert "Illawarra Group" not in leftover
    assert "ava at dapto dentists" not in lowered
    assert "ava at shellharbour dentists" not in lowered
    assert "ava at woonona dentists" not in lowered
    assert "shellharbour" in lowered
    assert "dapto" in lowered
    assert "woonona" in lowered
    assert "warm" in lowered or "human" in lowered
    assert "booking" in lowered or "book" in lowered
    assert "one" in lowered and "question" in lowered
    assert "short" in lowered
    assert "debto" not in lowered
    assert "winona" not in lowered
    assert len(text) < 500


def test_inbound_greeting_identity_ignores_mapped_branch() -> None:
    """DID/portal branch is not the number the caller reached."""
    for branch_id in ("shellharbour", "dapto", "woonona"):
        text = inbound_greeting_instructions(branch_id)
        lowered = text.lower()
        assert "ava at illawarra dentists" in lowered
        leftover = text.replace(GROUP_NAME, "")
        assert "Illawarra Dental" not in leftover
        assert f"ava at {branch_id} dentists" not in lowered


def test_ava_session_uses_realtime_llm_and_interruptions() -> None:
    from agent import _build_ava_session

    source = inspect.getsource(_build_ava_session)
    assert "realtime_llm" in source
    assert "enabled" in source
    assert "True" in source


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
