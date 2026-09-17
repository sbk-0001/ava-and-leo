"""Ava must react to the caller's voice, not the room behind them.

18 Sep 2026: callers in noisy places had Ava cut off and reply to noise. Krisp
background-voice cancellation (telephony model on SIP) was already on; OpenAI's
own noise reduction was off and server VAD fired at threshold 0.5, so traffic,
TVs and other voices counted as the caller starting to speak.
"""

from __future__ import annotations

import pytest

from agent import resolve_noise_cancellation


def _model(monkeypatch, **env):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for key in ("AVA_VAD_THRESHOLD", "AVA_NOISE_REDUCTION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from ava_receptionist import ava_realtime_model

    return ava_realtime_model()


def test_openai_noise_reduction_is_on_for_phone_audio(monkeypatch) -> None:
    opts = _model(monkeypatch)._opts
    reduction = opts.input_audio_noise_reduction
    assert reduction is not None
    assert getattr(reduction, "type", None) == "near_field"


def test_speech_detection_needs_a_clear_voice(monkeypatch) -> None:
    detection = _model(monkeypatch)._opts.turn_detection
    assert detection.type == "server_vad"
    assert detection.threshold >= 0.75
    assert detection.interrupt_response is True, "callers can still cut in"
    assert detection.prefix_padding_ms >= 300


def test_settings_can_be_tuned_without_a_code_change(monkeypatch) -> None:
    opts = _model(
        monkeypatch, AVA_VAD_THRESHOLD="0.85", AVA_NOISE_REDUCTION="far_field"
    )._opts
    assert opts.turn_detection.threshold == pytest.approx(0.85)
    assert opts.input_audio_noise_reduction.type == "far_field"

    off = _model(monkeypatch, AVA_NOISE_REDUCTION="off", AVA_VAD_THRESHOLD="7")._opts
    assert off.input_audio_noise_reduction is None
    assert off.turn_detection.threshold == pytest.approx(0.75), "bad values fall back"


def test_background_voice_cancellation_stays_on_by_default() -> None:
    assert resolve_noise_cancellation(env={}) is not None


@pytest.mark.parametrize("text", ["mm", "uh", "hmm", ""])
def test_noise_shaped_transcripts_are_ignored(text: str) -> None:
    from turn_filter import classify_user_turn

    assert classify_user_turn(text, tool_in_flight=False).ignore is True
