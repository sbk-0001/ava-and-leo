"""Live never-silent recorder: owner scenarios, egress request, WAV metrics."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from never_silent_harness import OWNER_SCENARIOS, live_audio_ready
from never_silent_recorder import (
    SAMPLE_RATE,
    _wav_to_pcm,
    analyze_pcm,
    build_caller_token,
    build_dispatch_metadata,
    build_room_composite_request,
    egress_s3_from_env,
    stock_phrases_from_texts,
    write_wav,
)


def _tone(frames: int, *, amplitude: int = 8000, sample_rate: int = 24000) -> bytes:
    samples = [
        int(amplitude * math.sin(2 * math.pi * 440 * i / sample_rate))
        for i in range(frames)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


def _silence(frames: int) -> bytes:
    return b"\x00\x00" * frames


def test_owner_scenarios_are_the_eight_requested() -> None:
    assert len(OWNER_SCENARIOS) == 8
    slugs = [row.slug for row in OWNER_SCENARIOS]
    assert slugs == [
        "01-slow-availability",
        "02-tool-timeout",
        "03-toothache-monologue",
        "04-barge-in-three",
        "05-bot-ask-twice",
        "06-unknown-fee",
        "07-swollen-swallow",
        "08-cancel-24h-fee",
    ]
    texts = " ".join(s.title.lower() + " " + s.goal.lower() for s in OWNER_SCENARIOS)
    assert "6s" in texts or "6 s" in texts or "six" in texts
    assert "stage 5" in texts
    assert "20s" in texts or "20 s" in texts or "twenty" in texts
    assert "three" in texts and "interrupt" in texts
    assert "real person" in texts
    assert "fee table" in texts or "price" in texts
    assert "swallow" in texts
    assert "50" in texts or "fifty" in texts


def test_each_owner_scenario_has_scripted_caller_turns() -> None:
    for spec in OWNER_SCENARIOS:
        assert spec.turns, spec.slug
        spoken = " ".join(turn.text for turn in spec.turns).lower()
        assert spoken.strip(), spec.slug
    slow = OWNER_SCENARIOS[0]
    assert slow.booking_delay_s == pytest.approx(6.0)
    timeout = OWNER_SCENARIOS[1]
    assert timeout.booking_hang_s >= 12
    mono = OWNER_SCENARIOS[2]
    assert any((turn.min_speak_s or 0) >= 18 for turn in mono.turns)
    barge = OWNER_SCENARIOS[3]
    assert sum(1 for turn in barge.turns if turn.interrupt_after_s) == 3
    bot = OWNER_SCENARIOS[4]
    assert (
        sum(
            "real person" in t.text.lower() or "bot" in t.text.lower()
            for t in bot.turns
        )
        >= 2
    )
    fee = OWNER_SCENARIOS[5]
    assert "filling" in " ".join(t.text.lower() for t in fee.turns)
    emergency = OWNER_SCENARIOS[6]
    assert "swallow" in " ".join(t.text.lower() for t in emergency.turns)
    cancel = OWNER_SCENARIOS[7]
    assert cancel.seed_cancel_24h is True


def test_room_composite_request_is_audio_only_ogg() -> None:
    req = build_room_composite_request(
        room_name="ava-ns-test",
        filepath="never-silent/01-slow-availability.ogg",
    )
    assert req.room_name == "ava-ns-test"
    assert req.audio_only is True
    assert not getattr(req, "layout", "")
    files = list(req.file_outputs)
    assert files
    assert "01-slow-availability.ogg" in files[0].filepath
    file_type = files[0].file_type
    assert "OGG" in str(file_type) or int(file_type) == 2


def test_egress_s3_from_env_optional() -> None:
    assert egress_s3_from_env(env={}) is None
    s3 = egress_s3_from_env(
        env={
            "EGRESS_S3_BUCKET": "recordings",
            "EGRESS_S3_ACCESS_KEY": "AKIA",
            "EGRESS_S3_SECRET": "secret",
            "EGRESS_S3_REGION": "ap-southeast-2",
        }
    )
    assert s3 is not None
    assert s3.bucket == "recordings"


def test_dispatch_metadata_forces_ava_and_pacing() -> None:
    raw = build_dispatch_metadata(OWNER_SCENARIOS[0])
    assert '"persona": "ava"' in raw
    assert '"booking_delay_s": 6.0' in raw or '"booking_delay_s": 6' in raw
    hang = build_dispatch_metadata(OWNER_SCENARIOS[1])
    assert "booking_hang_s" in hang
    cancel = build_dispatch_metadata(OWNER_SCENARIOS[7])
    assert '"seed_cancel_24h": true' in cancel


def test_caller_token_can_publish_and_subscribe() -> None:
    token = build_caller_token(
        url="wss://example.livekit.cloud",
        api_key="devkey",
        api_secret="secret" * 4,
        room="ava-ns-demo",
        identity="scripted-caller",
    )
    assert token
    assert isinstance(token, str)
    assert len(token) > 20


def test_wav_to_pcm_resamples_stereo_8bit_without_audioop() -> None:
    import io

    frames = 4800
    stereo = bytearray()
    for index in range(frames):
        left = 128 + int(40 * math.sin(2 * math.pi * 440 * index / 16000))
        stereo.extend((max(0, min(255, left)), 128))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(1)
        handle.setframerate(16000)
        handle.writeframes(bytes(stereo))
    pcm = _wav_to_pcm(buf.getvalue())
    expected = round(frames * SAMPLE_RATE / 16000)
    assert len(pcm) == expected * 2
    rms = (
        sum(sample * sample for sample in struct.unpack(f"<{expected}h", pcm))
        / expected
    ) ** 0.5
    assert rms > 100


def test_write_wav_is_real_riff(tmp_path: Path) -> None:
    pcm = _tone(2400)
    path = tmp_path / "01-slow-availability.wav"
    write_wav(path, pcm, sample_rate=24000, channels=1)
    assert path.stat().st_size > 44
    with path.open("rb") as handle:
        assert handle.read(4) == b"RIFF"
        handle.read(4)
        assert handle.read(4) == b"WAVE"
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 2400


def test_silence_metrics_exclude_caller_talking() -> None:
    rate = 24000
    win = int(rate * 0.02)
    agent = _silence(win * 50) + _tone(win * 10) + _silence(win * 20) + _tone(win * 10)
    quiet_caller = _silence(len(agent) // 2)
    talking_caller = _silence(win * 60) + _tone(win * 20) + _silence(win * 10)

    ttfa, gap = analyze_pcm(agent, quiet_caller, sample_rate=rate)
    assert 900 <= ttfa <= 1100
    assert 300 <= gap <= 500

    _ttfa2, gap_excl = analyze_pcm(agent, talking_caller, sample_rate=rate)
    assert gap_excl < 200


def test_stock_phrases_from_transcript() -> None:
    found = stock_phrases_from_texts(
        [
            "Righto, pulling up the diary — bit slow this morning.",
            "doo doo doo",
            "sorry, go on",
        ]
    )
    lowered = [line.lower() for line in found]
    assert any("doo doo doo" in line for line in lowered)
    assert any(
        "sorry, go on" in line or "pulling up the diary" in line for line in lowered
    )


def test_live_audio_ready_still_requires_all_four() -> None:
    assert live_audio_ready(env={}) is False
    assert (
        live_audio_ready(
            env={
                "LIVEKIT_URL": "wss://example.livekit.cloud",
                "LIVEKIT_API_KEY": "k",
                "LIVEKIT_API_SECRET": "s",
                "OPENAI_API_KEY": "sk",
            }
        )
        is True
    )
