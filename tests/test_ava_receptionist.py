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


def test_inbound_greeting_is_ava_group_not_single_clinic() -> None:
    """First line is Ava for the group, not locked to the dialled branch."""
    text = inbound_greeting_instructions("dapto")
    lowered = text.lower()
    assert "ava" in lowered
    assert "leo" not in lowered
    assert "shellharbour dentists group" in lowered
    assert "dapto" in lowered
    assert "barrack heights" in lowered
    assert "woonona" in lowered
    assert "warm" in lowered or "human" in lowered
    assert "lock" in lowered or "hint" in lowered
    assert "byte voice" in lowered
    assert "one" in lowered and "question" in lowered
    assert "short" in lowered
    assert len(text) < 700


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


def test_routing_tools_are_wired_on_ava() -> None:
    source = inspect.getsource(AvaReceptionist)
    assert "lookup_nearby_clinics" in source
    assert "lookup_clinician" in source
    assert "branch_id" in inspect.getsource(AvaReceptionist.get_availability)
    assert "branch_id" in inspect.getsource(AvaReceptionist.book_appointment)


def test_default_voice_choice_is_documented_marin() -> None:
    """marin stays: no AU Realtime voice exists; it is the recommended feminine ID."""
    import ava_receptionist as module

    source = inspect.getsource(module)
    assert 'AVA_DEFAULT_VOICE = "marin"' in source
    assert "coral" in source.lower()
    assert "no AU-specific" in source or "no au-specific" in source.lower()
