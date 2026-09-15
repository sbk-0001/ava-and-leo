"""FillerLadder: speak-before-dispatch, timings, no-repeat, interrupt, stage 5."""

from __future__ import annotations

import asyncio
import random

import pytest

from call_state import CallState
from filler_ladder import (
    FAST_PATH_S,
    JITTER_MS,
    STAGE_OFFSETS_MS,
    FakeClock,
    FakeSpeaker,
    FillerLadder,
    offset_with_jitter,
)
from phrase_pools import STAGE_1


class ZeroJitter(random.Random):
    def randint(self, a: int, b: int) -> int:
        del a, b
        return 0


class CountingBooking:
    def __init__(self) -> None:
        self.take_calls = 0
        self.messages: list[dict] = []

    async def take_message(self, **kwargs):
        self.take_calls += 1
        self.messages.append(kwargs)
        return {
            "ok": True,
            "confirmed": True,
            "message_id": f"msg_{self.take_calls}",
            "branch_id": kwargs.get("branch"),
        }


def _ladder(
    clock: FakeClock | None = None,
    booking: CountingBooking | None = None,
) -> tuple[FillerLadder, FakeClock, FakeSpeaker, CallState, CountingBooking]:
    clock = clock or FakeClock()
    speaker = FakeSpeaker(clock=clock)
    state = CallState(branch="shellharbour", phrase_rng=ZeroJitter(0))
    booking = booking or CountingBooking()
    ladder = FillerLadder(
        state,
        speaker=speaker,
        booking=booking,
        clock=clock,
        sleeper=clock.sleep,
        rng=ZeroJitter(1),
    )
    return ladder, clock, speaker, state, booking


def test_jitter_stays_within_300ms() -> None:
    rng = random.Random(0)
    for stage, base in STAGE_OFFSETS_MS.items():
        samples = [
            offset_with_jitter(base, jitter_ms=JITTER_MS, rng=rng, stage=stage)
            for _ in range(80)
        ]
        for delay in samples:
            ms = delay * 1000
            assert base - JITTER_MS <= ms <= base + JITTER_MS
            if stage == 1:
                assert delay >= 0


@pytest.mark.asyncio
async def test_first_audio_precedes_tool_dispatch() -> None:
    order: list[str] = []
    ladder, _clock, speaker, _state, _booking = _ladder()

    async def tool():
        order.append("tool")
        return {"ok": True, "slots": []}

    result, trace = await ladder.dispatch(tool)
    assert result["ok"] is True
    assert order[0] == "tool"
    assert speaker.spoken
    assert speaker.spoken[0] in STAGE_1
    assert trace.first_audio_ts is not None
    assert trace.tool_dispatch_ts is not None
    assert trace.first_audio_ts < trace.tool_dispatch_ts
    assert trace.audio_before_dispatch is True
    assert trace.stages_spoken[0] == 1
    assert speaker.finish_word_calls >= 1
    assert trace.finished_word_before_result is True


@pytest.mark.asyncio
async def test_ladder_timings_and_no_repeat() -> None:
    ladder, clock, speaker, state, _booking = _ladder()
    hang = asyncio.Event()
    stage_times: list[tuple[int, float]] = []

    orig_speak = ladder.speak_stage

    async def tracked(stage: int) -> str:
        line = await orig_speak(stage)
        stage_times.append((stage, clock.t))
        return line

    ladder.speak_stage = tracked  # type: ignore[method-assign]

    async def tool():
        await hang.wait()
        return {"ok": True}

    task = asyncio.create_task(ladder.dispatch(tool))
    # Let the ladder run to stage 5 on the fake clock.
    for _ in range(40):
        await asyncio.sleep(0)
        if ladder.trace.stage5_fallback:
            break
    result, _trace = await task
    assert result["stage5"] is True
    assert result["fallback"] == "take_message"
    spoken_stages = [stage for stage, _t in stage_times]
    assert spoken_stages == [1, 2, 3, 4, 5]
    # Stage 1 at t~0; later stages at dispatch origin + offset.
    times = dict(stage_times)
    origin = times[1]
    assert times[2] - origin == pytest.approx(1.2001, abs=0.05)
    assert times[3] - origin == pytest.approx(3.0001, abs=0.05)
    assert times[4] - origin == pytest.approx(6.0001, abs=0.05)
    assert times[5] - origin == pytest.approx(12.0001, abs=0.05)
    assert len(set(speaker.spoken)) == len(speaker.spoken)
    assert state.used_phrases["stage_1"]
    assert state.used_phrases["stage_2"]


@pytest.mark.asyncio
async def test_stage5_calls_take_message_for_real() -> None:
    booking = CountingBooking()
    ladder, _clock, _speaker, state, _ = _ladder(booking=booking)
    state.caller_name = "Jordan"
    state.caller_mobile = "0413000111"

    async def never():
        await asyncio.Event().wait()
        return {"ok": True}

    result, trace = await ladder.dispatch(never)
    assert trace.stage5_fallback is True
    assert booking.take_calls == 1
    assert booking.messages[0]["name"] == "Jordan"
    assert booking.messages[0]["mobile"] == "0413000111"
    assert result["fallback"] == "take_message"
    assert state.intent == "message"


@pytest.mark.asyncio
async def test_caller_interrupt_restarts_from_stage_2() -> None:
    ladder, _clock, speaker, _state, _booking = _ladder()
    hang = asyncio.Event()

    async def tool():
        await hang.wait()
        return {"ok": True, "slots": ["x"]}

    task = asyncio.create_task(ladder.dispatch(tool))
    for _ in range(20):
        await asyncio.sleep(0)
        if 2 in ladder.trace.stages_spoken:
            break
    assert 2 in ladder.trace.stages_spoken
    await ladder.on_caller_speech()
    assert ladder._pending
    assert ladder._pending[0][1] == 2
    hang.set()
    result, trace = await task
    assert result["ok"] is True
    assert trace.caller_interrupted is True
    assert trace.stages_spoken.count(1) == 1
    assert speaker.spoken[0] in STAGE_1


@pytest.mark.asyncio
async def test_pool_does_not_repeat_within_a_call() -> None:
    ladder, _clock, speaker, state, _booking = _ladder()

    async def fast():
        return {"ok": True}

    for _ in range(len(STAGE_1)):
        await ladder.dispatch(fast)
    assert len(state.used_phrases["stage_1"]) == len(STAGE_1)
    assert len(set(state.used_phrases["stage_1"])) == len(STAGE_1)
    # Wrap is allowed only after the pool is exhausted.
    await ladder.dispatch(fast)
    assert speaker.spoken[0] in STAGE_1


@pytest.mark.asyncio
async def test_cached_fast_path_cancels_after_stage_1() -> None:
    """Cached / sub-300ms results must not keep scrolling through later stages."""
    speaker = FakeSpeaker()
    state = CallState(branch="shellharbour", phrase_rng=ZeroJitter(0))
    ladder = FillerLadder(
        state,
        speaker=speaker,
        booking=CountingBooking(),
        rng=ZeroJitter(1),
    )

    async def cached_tool():
        await asyncio.sleep(0.05)
        return {
            "ok": True,
            "cached": True,
            "slots": [
                {
                    "slot_id": "s1",
                    "date": "2026-09-22",
                    "time": "10:00",
                    "clinician": "Dr Mohit Tolani",
                }
            ],
        }

    t0 = asyncio.get_event_loop().time()
    result, trace = await ladder.dispatch(cached_tool)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result["cached"] is True
    assert FAST_PATH_S == 0.3
    assert trace.stages_spoken == [1]
    assert trace.fast_path is True
    assert elapsed < 0.5
    assert 2 not in trace.stages_spoken
