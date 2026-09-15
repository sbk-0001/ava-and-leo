"""Room noise cancellation must not silence Ava when enhancer auth is missing."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from livekit import rtc
from livekit.plugins import noise_cancellation

from agent import _room_options, resolve_noise_cancellation
from sip_utils import is_sip_participant


def _cloud_env(**extra: str) -> dict[str, str]:
    env = {
        "LIVEKIT_URL": "wss://demo.livekit.cloud",
        "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "devsecret",
    }
    env.update(extra)
    return env


def test_nc_off_attaches_no_filter() -> None:
    assert resolve_noise_cancellation(env={"AVA_NOISE_CANCELLATION": "off"}) is None
    opts = _room_options(env={"AVA_NOISE_CANCELLATION": "off"})
    audio = getattr(opts, "audio_input", None)
    assert audio is None or getattr(audio, "noise_cancellation", None) is None


def test_default_krisp_is_not_ai_coustics() -> None:
    """Call Ava uses LiveKit Cloud Krisp BVC, not ai-coustics (that hung session.start)."""
    filt = resolve_noise_cancellation(env=_cloud_env())
    assert filt is not None
    assert callable(filt)
    web = filt(
        SimpleNamespace(
            participant=SimpleNamespace(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            )
        )
    )
    sip = filt(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
        )
    )
    assert web.options["modelPath"] == noise_cancellation.BVC().options["modelPath"]
    assert (
        sip.options["modelPath"]
        == noise_cancellation.BVCTelephony().options["modelPath"]
    )
    opts = _room_options(env=_cloud_env())
    assert opts.audio_input is not None
    assert opts.audio_input.noise_cancellation is not None


def test_ai_coustics_without_license_does_not_attach_enhancer() -> None:
    """Missing enhancer auth previously published 0 audio tracks. Do not re-arm that."""
    filt = resolve_noise_cancellation(
        env=_cloud_env(AVA_NOISE_CANCELLATION="ai_coustics")
    )
    assert filt is not None
    assert callable(filt)
    chosen = filt(SimpleNamespace(participant=None))
    assert chosen.options["modelPath"] == noise_cancellation.BVC().options["modelPath"]


def test_ai_coustics_with_license_uses_enhancer() -> None:
    filt = resolve_noise_cancellation(
        env=_cloud_env(
            AVA_NOISE_CANCELLATION="ai_coustics",
            AI_COUSTICS_LICENSE_KEY="license-test",
        )
    )
    assert filt is not None
    assert not callable(filt)
    assert type(filt).__name__ == "AICousticsAudioEnhancer"


def test_krisp_selector_treats_magicmock_as_web_not_sip() -> None:
    filt = resolve_noise_cancellation(env=_cloud_env())
    chosen = filt(SimpleNamespace(participant=MagicMock()))
    assert chosen.options["modelPath"] == noise_cancellation.BVC().options["modelPath"]
    assert is_sip_participant(MagicMock()) is False


def test_nc_on_with_one_uses_krisp() -> None:
    filt = resolve_noise_cancellation(env={"AVA_NOISE_CANCELLATION": "1"})
    assert filt is not None
    assert callable(filt)


def test_nc_runtime_failure_returns_empty_room_options(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("plugin failed")

    monkeypatch.setattr(noise_cancellation, "BVC", _boom)
    assert resolve_noise_cancellation(env={"AVA_NOISE_CANCELLATION": "1"}) is None
    opts = _room_options(env={"AVA_NOISE_CANCELLATION": "1"})
    audio = getattr(opts, "audio_input", None)
    assert audio is None or getattr(audio, "noise_cancellation", None) is None
