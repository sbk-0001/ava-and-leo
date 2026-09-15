"""Never-silent harness writes metrics for the eight owner scenarios."""

from pathlib import Path

import pytest

from never_silent_harness import (
    OWNER_SCENARIOS,
    live_audio_ready,
    run_all,
    write_metrics,
)


def test_live_audio_ready_requires_all_secrets() -> None:
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


@pytest.mark.asyncio
async def test_never_silent_metrics(tmp_path: Path) -> None:
    rows = await run_all(tmp_path)
    assert len(rows) == 8
    slugs = {row.slug for row in rows}
    assert slugs == {spec.slug for spec in OWNER_SCENARIOS}
    for row in rows:
        if row.slug in {
            "01-slow-availability",
            "02-tool-timeout",
            "06-unknown-fee",
            "07-swollen-swallow",
            "08-cancel-24h-fee",
        }:
            assert row.first_audio_before_dispatch is True
        assert row.longest_silence_gap_ms < 800
        assert row.audio_recording is None
    timeout = next(row for row in rows if row.slug == "02-tool-timeout")
    assert timeout.stage5_fallback is True
    cancel = next(row for row in rows if row.slug == "08-cancel-24h-fee")
    assert any("50" in note or "fee" in note.lower() for note in cancel.notes)
    metrics = (tmp_path / "metrics.json").read_text()
    assert "time_to_first_audio_ms" in metrics
    assert "stock_phrases_used" in metrics
    assert ".wav" not in metrics or 'audio_recording": null' in metrics
    readme = (tmp_path / "README.md").read_text()
    assert "Never-silent" in readme
    assert "uv run python src/never_silent_harness.py --live" in readme


def test_metrics_json_uses_real_wav_paths_only(tmp_path: Path) -> None:
    from never_silent_harness import CallMetrics

    wav = tmp_path / "01-slow-availability.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 8 + b"WAVE" + b"\x00" * 80)
    rows = [
        CallMetrics(
            slug="01-slow-availability",
            title="slow",
            time_to_first_audio_ms=410.0,
            longest_silence_gap_ms=620.0,
            stock_phrases_used=["doo doo doo"],
            audio_recording=str(wav),
        ),
        CallMetrics(
            slug="02-tool-timeout",
            title="timeout",
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            audio_recording="02-tool-timeout.wav",
        ),
    ]
    payload = write_metrics(rows, tmp_path, live_audio=True)
    calls = {row["slug"]: row for row in payload["calls"]}
    assert calls["01-slow-availability"]["audio_recording"] == str(wav)
    assert calls["02-tool-timeout"]["audio_recording"] is None
