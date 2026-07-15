"""The resolved persona name must be baked into the agent's own instructions.

Regression: previously AGENT_PERSONA only chose the TTS voice and the one-off
greeting line, so the model didn't actually KNOW its name (e.g. couldn't answer
"what's your name?" as Leo). These are static-string checks — no network.
"""

from __future__ import annotations

import agent


def test_leo_persona_knows_its_name():
    instructions = agent.Assistant(agent_name="leo").instructions
    assert "name is Leo" in instructions
    assert "Ava" not in instructions


def test_ava_persona_knows_its_name():
    instructions = agent.Assistant(agent_name="ava").instructions
    assert "name is Ava" in instructions
    assert "Leo" not in instructions


def test_default_persona_matches_default_constant():
    instructions = agent.Assistant().instructions
    assert f"name is {agent.DEFAULT_PERSONA.capitalize()}" in instructions
