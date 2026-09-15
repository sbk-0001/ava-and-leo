"""Ava Realtime voice defaults, barge-in, tools, and branch greeting."""

import inspect

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
    assert "kill_switch" in source
    assert "inbound_greeting_instructions(call_state.branch)" in source
    assert "is_rate_limit_error" in source
    assert "RateLimitRecovery" in source
    assert "CachedBookingProvider" in source
    assert "AmbientBed" in source
    assert "attach_backchannels" in source
    assert "mark_interrupted" in source
