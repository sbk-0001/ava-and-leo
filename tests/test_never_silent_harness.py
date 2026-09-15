"""Never-silent harness writes metrics for the eight owner scenarios."""

from pathlib import Path

import pytest

from never_silent_harness import live_audio_ready, run_all


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
    assert "01-new-patient-checkup" in slugs
    assert "08-barge-in-three-times" in slugs
    for row in rows:
        assert row.first_audio_before_dispatch is True
        assert row.longest_silence_gap_ms < 800
        assert row.stock_phrases_used or row.slug in {
            "05-bot-ask-twice",
            "08-barge-in-three-times",
        }
    metrics = (tmp_path / "metrics.json").read_text()
    assert "time_to_first_audio_ms" in metrics
    assert "stock_phrases_used" in metrics
    readme = (tmp_path / "README.md").read_text()
    assert "Never-silent" in readme
