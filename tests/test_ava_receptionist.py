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
    """Call Ava desk uses conversation_item_added + function_tools_executed.

    Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
          https://docs.livekit.io/reference/agents/events/#function_tools_executed
    """
    from agent import _register_desk_feed, my_agent

    source = inspect.getsource(_register_desk_feed) + inspect.getsource(my_agent)
    assert "conversation_item_added" in source
    assert "function_tools_executed" in source
    assert "_register_desk_feed" in inspect.getsource(my_agent)
    assert 'persona_key == "ava"' in inspect.getsource(my_agent)


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
