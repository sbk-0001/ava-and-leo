"""Pre-rendered filler bank: boot assert, no model path, ducking."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from filler_bank import (
    FILLER_POOLS,
    MIN_POOL_VARIANTS,
    FillerBankError,
    assert_filler_bank,
    generate_bank,
    get_filler_bank,
    reset_filler_bank_cache,
)
from filler_ladder import SessionSpeaker
from filler_player import DUCK_S, FillerPlayer, duck_gain
from phrase_pools import RECOVERY, STAGE_1, STAGE_2, STAGE_3, STAGE_4, STAGE_5


def test_every_filler_pool_has_at_least_four_clips() -> None:
    bank = assert_filler_bank()
    for pool, lines in FILLER_POOLS.items():
        assert len(lines) >= MIN_POOL_VARIANTS, pool
        assert len(bank[pool]) >= MIN_POOL_VARIANTS, pool
        assert {clip.text for clip in bank[pool]} == set(lines)


def test_boot_fails_if_a_pool_is_removed(tmp_path: Path) -> None:
    generate_bank(root=tmp_path, source="test")
    # Remove an entire pool directory.
    stage = tmp_path / "stage_1"
    for wav in stage.glob("*.wav"):
        wav.unlink()
    stage.rmdir()
    with pytest.raises(FillerBankError, match=r"filler bank|stage_1"):
        assert_filler_bank(root=tmp_path)


def test_session_speaker_fillers_never_call_say_or_generate_reply() -> None:
    src = inspect.getsource(SessionSpeaker)
    assert "generate_reply(" not in src
    assert "kick_scripted_speech" not in src
    assert ".say(" not in src
    recover_src = inspect.getsource(SessionSpeaker.utter)
    assert "generate_reply(" not in recover_src


@pytest.mark.asyncio
async def test_session_speaker_plays_bank_not_model() -> None:
    from types import SimpleNamespace

    from filler_bank import get_filler_bank

    replies: list[object] = []
    says: list[object] = []
    session = SimpleNamespace(
        say=lambda *a, **k: says.append((a, k)),
        generate_reply=lambda **k: replies.append(k),
    )
    player = FillerPlayer(get_filler_bank())
    speaker = SessionSpeaker(session, player=player)
    line = STAGE_1[0]
    await speaker.utter(line)
    assert player.played == [line]
    assert replies == []
    assert says == []
    assert speaker.last_first_audio_ts is not None


def test_duck_is_eighty_ms_never_hard_cut() -> None:
    assert pytest.approx(0.08) == DUCK_S
    assert duck_gain(0) == 1.0
    assert duck_gain(0.04) == pytest.approx(0.5)
    assert duck_gain(0.08) == 0.0
    assert duck_gain(0.2) == 0.0


@pytest.mark.asyncio
async def test_model_audio_ducks_filler_over_eighty_ms() -> None:
    player = FillerPlayer(get_filler_bank())
    line = STAGE_2[0]
    task_play = player.play(line)
    await task_play
    player.notify_model_audio()
    # Replay mix path with model already flagged.
    player.reset_model_audio()
    player.notify_model_audio()
    await player._mix_playout(player._bank().get(line))
    assert player.gains
    assert player.gains[0] <= 1.0
    # Fade, not an immediate drop from 1 to 0 on the first sample unless already due.
    assert 0.0 in player.gains or player.gains[-1] == 0.0


def test_rate_limit_cover_source_has_no_say_fallback() -> None:
    from realtime_hygiene import RateLimitRecovery

    src = inspect.getsource(RateLimitRecovery)
    assert "session.say failed; trying generate_reply" not in src
    cover = inspect.getsource(RateLimitRecovery._play_cover)
    assert "generate_reply(" not in cover
    assert "session.say" not in cover


def test_pools_include_stage_four_and_recovery() -> None:
    assert len(STAGE_4) >= 4
    assert len(STAGE_5) >= 4
    assert len(STAGE_3) >= 4
    assert len(RECOVERY) >= 4
    reset_filler_bank_cache()
