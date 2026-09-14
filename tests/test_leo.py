"""Leo Realtime voice defaults and console-safe EndCallTool."""

import inspect

from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import openai

from leo import (
    LEO_DEFAULT_VOICE,
    LeoReceptionist,
    leo_realtime_model,
    resolve_leo_voice,
    should_offer_inbound_greeting,
)


def test_default_realtime_voice_is_marin() -> None:
    assert LEO_DEFAULT_VOICE == "marin"
    assert resolve_leo_voice(env={}) == "marin"
    assert resolve_leo_voice(env={"LEO_REALTIME_VOICE": ""}) == "marin"
    assert resolve_leo_voice(env={"LEO_REALTIME_VOICE": "cedar"}) == "cedar"


def test_end_call_tool_ignores_on_enter() -> None:
    source = inspect.getsource(LeoReceptionist.__init__)
    assert "ignore_on_enter" in source
    assert "ignore_on_enter" in inspect.signature(EndCallTool.__init__).parameters


def test_realtime_model_uses_verified_openai_kwargs_only() -> None:
    """Keep RealtimeModel construction simple so the session does not crash.

    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/
    """
    source = inspect.getsource(leo_realtime_model)
    signature = inspect.signature(openai.realtime.RealtimeModel.__init__)
    assert "model" in signature.parameters
    assert "voice" in signature.parameters
    assert "turn_detection" in signature.parameters
    assert "speed" in signature.parameters
    assert "RealtimeModel(" in source
    assert "model=" in source
    assert "voice=" in source
    # Do not pass unverified experimental kwargs; barge-in uses plugin defaults.
    assert "interrupt_response" not in source
    assert "eagerness" not in source


def test_dental_receptionist_greets_on_portal_and_inbound() -> None:
    assert should_offer_inbound_greeting(persona_key="leo", outbound=False) is True
    assert should_offer_inbound_greeting(persona_key="leo", outbound=True) is False
    assert (
        should_offer_inbound_greeting(
            persona_key="ava",
            outbound=False,
            metadata={"source": "portal"},
        )
        is True
    )
    assert should_offer_inbound_greeting(persona_key="ava", outbound=False) is False
