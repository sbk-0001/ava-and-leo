"""Startup asserts: SIP host, live OpenAI key, 429 circuit breaker stub."""

import pytest

from startup_checks import (
    DEFAULT_SIP_HOST,
    RateLimitCircuitBreaker,
    assert_live_openai_key,
    assert_sip_host,
    expected_sip_host,
)


def test_expected_sip_host_default() -> None:
    assert expected_sip_host({}) == DEFAULT_SIP_HOST
    assert DEFAULT_SIP_HOST == "5sft82r5337.sip.livekit.cloud"
    assert (
        expected_sip_host({"EXPECTED_SIP_HOST": "other.sip.livekit.cloud"})
        == "other.sip.livekit.cloud"
    )


def test_assert_sip_host_passes_when_unset_or_matching() -> None:
    assert assert_sip_host({}) == DEFAULT_SIP_HOST
    assert assert_sip_host({"LIVEKIT_SIP_HOST": DEFAULT_SIP_HOST}) == DEFAULT_SIP_HOST


def test_assert_sip_host_rejects_mismatch() -> None:
    with pytest.raises(RuntimeError, match="SIP host"):
        assert_sip_host({"LIVEKIT_SIP_HOST": "wrong.sip.livekit.cloud"})


def test_live_openai_key_must_differ_from_demo_harness() -> None:
    assert_live_openai_key({"OPENAI_API_KEY": "sk-live"})
    assert_live_openai_key(
        {
            "OPENAI_API_KEY": "sk-live",
            "OPENAI_LIVE_API_KEY": "sk-live",
            "OPENAI_DEMO_API_KEY": "sk-demo",
        }
    )
    with pytest.raises(RuntimeError, match="DEMO/HARNESS"):
        assert_live_openai_key(
            {
                "OPENAI_API_KEY": "sk-same",
                "OPENAI_HARNESS_API_KEY": "sk-same",
            }
        )
    with pytest.raises(RuntimeError, match="DEMO/HARNESS"):
        assert_live_openai_key(
            {
                "OPENAI_LIVE_API_KEY": "sk-same",
                "OPENAI_DEMO_API_KEY": "sk-same",
            }
        )


def test_circuit_breaker_opens_after_repeated_429s() -> None:
    breaker = RateLimitCircuitBreaker(threshold=3)
    env = {"OVERFLOW_NUMBER": "+61242169911"}
    assert breaker.record_429() is False
    assert breaker.record_429() is False
    assert breaker.record_429() is True
    assert breaker.open is True
    assert breaker.overflow_number(env) == "+61242169911"


def test_filler_bank_boot_assert_is_wired() -> None:
    from startup_checks import assert_filler_audio_bank

    assert_filler_audio_bank({"FILLER_ALLOW_SYNTHETIC": "1"})
    from filler_bank import SYNTHETIC_SOURCE, FillerBankError, assert_filler_bank

    assert callable(assert_filler_bank)
    assert issubclass(FillerBankError, RuntimeError)
    assert SYNTHETIC_SOURCE == "synthetic-placeholder"
