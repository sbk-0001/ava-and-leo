"""Leo Realtime voice defaults and console-safe EndCallTool."""

import inspect

from livekit.agents.beta.tools import EndCallTool

from leo import LEO_DEFAULT_VOICE, LeoReceptionist, resolve_leo_voice


def test_default_realtime_voice_is_marin() -> None:
    assert LEO_DEFAULT_VOICE == "marin"
    assert resolve_leo_voice(env={}) == "marin"
    assert resolve_leo_voice(env={"LEO_REALTIME_VOICE": ""}) == "marin"
    assert resolve_leo_voice(env={"LEO_REALTIME_VOICE": "cedar"}) == "cedar"


def test_end_call_tool_ignores_on_enter() -> None:
    source = inspect.getsource(LeoReceptionist.__init__)
    assert "ignore_on_enter" in source
    assert "ignore_on_enter" in inspect.signature(EndCallTool.__init__).parameters
