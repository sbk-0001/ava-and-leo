"""Leo Realtime voice defaults and console-safe EndCallTool."""

import inspect

from livekit.agents.beta.tools import EndCallTool

from leo import (
    LEO_DEFAULT_VOICE,
    LeoReceptionist,
    inbound_greeting_instructions,
    resolve_leo_voice,
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


def test_inbound_greeting_feels_human() -> None:
    """First line should sound like a receptionist picking up, still short."""
    text = inbound_greeting_instructions("dapto")
    lowered = text.lower()
    assert "leo" in lowered
    assert "dapto dentists" in lowered
    assert "dapto" in lowered
    assert "warm" in lowered or "human" in lowered
    assert "booking" in lowered
    assert "one" in lowered and "question" in lowered
    assert len(text) < 400
