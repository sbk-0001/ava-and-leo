"""Code-timed filler ladder. The model does not decide when to speak.

Timings from tool-dispatch origin, ±300ms jitter:
  0ms    stage 1  — before the network request
  1200ms stage 2
  3000ms stage 3
  6000ms stage 4
  12000ms stage 5 — take_message and end the wait

Docs: https://docs.livekit.io/agents/logic/tools/design/#speak-during-long-tool-calls
      https://docs.livekit.io/agents/multimodality/audio/
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from call_state import CallState
from phrase_pools import STAGE_EMPTY, STAGE_ERROR, STAGE_POOLS

logger = logging.getLogger("ava.filler")


def classify_dispatch_path(result: Mapping[str, Any] | None) -> str:
    """LATENCY | ERROR | EMPTY. UNKNOWN availability is EMPTY, not chockers."""
    if not isinstance(result, Mapping):
        return "ERROR"
    reason = str(result.get("reason") or "").lower()
    status = str(result.get("status") or "")
    if _is_rate_limit_payload(result) or reason in {
        "timeout",
        "practice_software_unavailable",
        "zavy360_unavailable",
        "zavy360_error",
    }:
        return "ERROR"
    if status == "UNKNOWN" or (not result.get("ok") and not result.get("slots")):
        return "ERROR" if not result.get("ok") else "EMPTY"
    if result.get("ok") and not list(result.get("slots") or []):
        return "EMPTY"
    return "LATENCY"


def _is_rate_limit_payload(result: Mapping[str, Any] | BaseException | None) -> bool:
    if result is None:
        return False
    if isinstance(result, BaseException):
        text = str(result).lower()
        return "429" in text or "rate_limit" in text
    reason = str(result.get("reason") or "").lower()
    status = str(result.get("status") or "")
    return (
        "429" in reason
        or "rate_limit" in reason
        or status == "429"
        or result.get("http_status") == 429
    )


STAGE_OFFSETS_MS: dict[int, int] = {
    1: 0,
    2: 1200,
    3: 3000,
    4: 6000,
    5: 12000,
}
JITTER_MS = 300
FAST_PATH_S = 0.3
WORD_DURATION_S = 0.45  # ~0.9 speech rate, one word then the result


class Speaker(Protocol):
    last_first_audio_ts: float | None
    last_text: str | None

    async def utter(self, text: str, *, allow_interruptions: bool = True) -> None: ...

    async def finish_current_word(self, word_s: float = WORD_DURATION_S) -> None: ...


@dataclass
class DispatchTrace:
    first_audio_ts: float | None = None
    tool_dispatch_ts: float | None = None
    stages_spoken: list[int] = field(default_factory=list)
    lines_spoken: list[str] = field(default_factory=list)
    stage5_fallback: bool = False
    caller_interrupted: bool = False
    finished_word_before_result: bool = False
    fast_path: bool = False
    path: str = "LATENCY"
    abandoned_on_429: bool = False
    retries: int = 0

    @property
    def audio_before_dispatch(self) -> bool:
        if self.first_audio_ts is None or self.tool_dispatch_ts is None:
            return False
        return self.first_audio_ts < self.tool_dispatch_ts


class FakeClock:
    """Deterministic clock for tests. sleep() jumps time and yields."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.t += seconds
        await asyncio.sleep(0)


class FakeSpeaker:
    """Records utterances and marks first-audio immediately."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.perf_counter
        self.spoken: list[str] = []
        self.last_first_audio_ts: float | None = None
        self.last_text: str | None = None
        self.started_at: float | None = None
        self.finish_word_calls = 0
        self.word_s = WORD_DURATION_S

    async def utter(self, text: str, *, allow_interruptions: bool = True) -> None:
        del allow_interruptions
        self.last_text = text
        self.started_at = self.clock()
        self.last_first_audio_ts = self.started_at
        self.spoken.append(text)

    async def finish_current_word(self, word_s: float = WORD_DURATION_S) -> None:
        self.finish_word_calls += 1
        if self.started_at is None or not self.last_text:
            return
        elapsed = self.clock() - self.started_at
        remaining = _remaining_to_word_boundary(elapsed, word_s)
        if remaining > 0:
            # Tests inject FakeClock via the ladder sleeper, not here.
            await asyncio.sleep(0)


def _remaining_to_word_boundary(elapsed: float, word_s: float) -> float:
    if word_s <= 0:
        return 0.0
    if elapsed <= 0:
        return 0.0
    next_boundary = math.ceil(elapsed / word_s) * word_s
    remaining = next_boundary - elapsed
    return max(0.0, min(word_s, remaining))


def offset_with_jitter(
    base_ms: int,
    *,
    jitter_ms: int = JITTER_MS,
    rng: random.Random | None = None,
    stage: int | None = None,
) -> float:
    """Return delay in seconds. Stage 1 is clamped to ≥0 so audio can start immediately."""
    chooser = rng or random.Random()
    jitter = chooser.randint(-jitter_ms, jitter_ms)
    ms = base_ms + jitter
    if stage == 1 or base_ms == 0:
        ms = max(0, ms)
    return ms / 1000.0


class SessionSpeaker:
    """Kick filler audio via session.say, falling back to generate_reply on Realtime.

    Docs: https://docs.livekit.io/agents/multimodality/audio/#session-say
          Realtime models need TTS for say(); otherwise generate_reply.
    """

    def __init__(
        self,
        session: Any,
        *,
        clock: Callable[[], float] | None = None,
        gate: Callable[[str], str] | None = None,
    ) -> None:
        self.session = session
        self.clock = clock or time.perf_counter
        self.gate = gate
        self.last_first_audio_ts: float | None = None
        self.last_text: str | None = None
        self.started_at: float | None = None
        self._handle: Any = None

    async def utter(self, text: str, *, allow_interruptions: bool = True) -> None:
        if self.gate is not None:
            text = self.gate(text)
        self.last_text = text
        self.started_at = self.clock()
        audio_started = asyncio.Event()

        def _mark_audio(*_args: Any, **_kwargs: Any) -> None:
            audio_started.set()

        try:
            once = getattr(self.session, "once", None)
            if callable(once):
                once("speech_created", _mark_audio)
        except Exception:
            logger.exception("could not subscribe to speech_created")

        kicked = False
        try:
            say = getattr(self.session, "say", None)
            if callable(say):
                self._handle = say(text, allow_interruptions=allow_interruptions)
                kicked = True
        except TypeError:
            try:
                self._handle = self.session.say(text)
                kicked = True
            except Exception:
                kicked = False
        except Exception:
            kicked = False

        if not kicked:
            try:
                self._handle = self.session.generate_reply(
                    instructions=(
                        "Cover the pause. Say exactly this and nothing else, "
                        f"in character: {text} Do not invent a diary result."
                    )
                )
            except Exception:
                logger.exception("filler utter failed")

        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(audio_started.wait(), timeout=0.35)
        self.last_first_audio_ts = self.clock()

    async def finish_current_word(self, word_s: float = WORD_DURATION_S) -> None:
        """Do not hard-cut Ava. Wait until the current word boundary, then return."""
        if self.started_at is None:
            return
        remaining = _remaining_to_word_boundary(self.clock() - self.started_at, word_s)
        if remaining > 0:
            await asyncio.sleep(remaining)


class FillerLadder:
    def __init__(
        self,
        state: CallState,
        *,
        speaker: Speaker,
        booking: Any | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        take_message: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.state = state
        self.speaker = speaker
        self.booking = booking
        self.clock = clock or time.perf_counter
        self._sleep = sleeper or asyncio.sleep
        self.rng = rng or random.Random()
        self._take_message = take_message
        self._pending: list[tuple[float, int]] = []
        self._cycle = 0
        self._stop = False
        self.active = False
        self.trace = DispatchTrace()

    def _pick_stage_line(self, stage: int, *, kind: str = "LATENCY") -> str:
        if kind == "ERROR":
            pool = STAGE_ERROR
            label = "stage_error"
        elif kind == "EMPTY":
            pool = STAGE_EMPTY
            label = "stage_empty"
        else:
            pool = STAGE_POOLS[stage]
            label = f"stage_{stage}"
        return self.state.pick_phrase(label, pool, rng=self.rng)

    def _schedule(self, from_stage: int, *, from_dispatch: bool = False) -> None:
        origin = self.clock()
        base_ms = 0 if from_dispatch else STAGE_OFFSETS_MS[from_stage]
        pending: list[tuple[float, int]] = []
        for stage in range(from_stage, 6):
            delay_s = offset_with_jitter(
                STAGE_OFFSETS_MS[stage] - base_ms,
                rng=self.rng,
                stage=stage,
            )
            pending.append((origin + delay_s, stage))
        self._pending = pending
        self._cycle += 1

    async def speak_stage(self, stage: int, *, kind: str = "LATENCY") -> str:
        line = self._pick_stage_line(stage, kind=kind)
        await self.speaker.utter(line, allow_interruptions=True)
        self.trace.stages_spoken.append(stage)
        self.trace.lines_spoken.append(line)
        self.state.record_stock_phrase(line)
        return line

    async def on_caller_speech(self) -> None:
        """Cancel pending fillers, keep the tool running, restart from stage 2."""
        if not self.active:
            return
        self.trace.caller_interrupted = True
        self._schedule(from_stage=2)

    async def _error_fallback(self) -> dict[str, Any]:
        if not self.state.may_offer_callback():
            return {
                "ok": False,
                "stage5": True,
                "fallback": "transfer",
                "reason": "rate_limit_exceeded",
                "note": (
                    "Diary is not usable and a callback is not allowed on this call. "
                    "Stay on the line. Offer to put them through to a person. "
                    "Do not invent a slot."
                ),
            }
        return await self._take_message_fallback()

    async def _take_message_fallback(self) -> dict[str, Any]:
        if not self.state.may_offer_callback():
            return await self._error_fallback()
        payload: dict[str, Any] = {
            "ok": True,
            "stage5": True,
            "fallback": "take_message",
            "reason": "tool_wait_timeout",
            "note": (
                "The diary did not come back in time. A message was left for the "
                "team. Do not invent a slot. Confirm you'll have them call back."
            ),
        }
        taker = self._take_message
        if taker is None and self.booking is not None:
            taker = self.booking.take_message
        if taker is not None:
            result = await taker(
                branch=self.state.branch,
                name=self.state.caller_name or "unknown",
                mobile=self.state.caller_mobile or "",
                reason=self.state.intent
                or "Could not finish looking up the diary — please call back",
            )
            merged = dict(result)
            merged.update({k: v for k, v in payload.items() if k not in merged})
            merged["stage5"] = True
            merged["fallback"] = "take_message"
            payload = merged
        self.state.intent = "message"
        self.trace.stage5_fallback = True
        return payload

    async def dispatch(
        self,
        factory: Callable[[], Awaitable[MutableMapping[str, Any]]],
    ) -> tuple[MutableMapping[str, Any], DispatchTrace]:
        """Stage 1 audio, then the tool, then stages 2-5 until the tool returns."""
        self.active = True
        self._stop = False
        self.trace = DispatchTrace()
        try:
            await self.speak_stage(1)
            first = self.speaker.last_first_audio_ts
            if first is None:
                first = self.clock()
            self.trace.first_audio_ts = first
            # Guarantee a strict precede even on a frozen test clock.
            await self._sleep(0.0001)
            self.trace.tool_dispatch_ts = self.clock()
            logger.info(
                "first-audio-ts=%s tool-dispatch-ts=%s audio_before_dispatch=%s",
                self.trace.first_audio_ts,
                self.trace.tool_dispatch_ts,
                self.trace.audio_before_dispatch,
            )
            self.state.last_dispatch_trace = self.trace

            tool_task = asyncio.create_task(factory())
            await asyncio.sleep(0)
            self._schedule(from_stage=2, from_dispatch=True)

            while True:
                if tool_task.done():
                    try:
                        result = tool_task.result()
                    except Exception as exc:
                        if _is_rate_limit_payload(exc):
                            result = {
                                "ok": False,
                                "reason": "rate_limit_exceeded",
                                "http_status": 429,
                            }
                        else:
                            raise
                    elapsed = 0.0
                    if self.trace.tool_dispatch_ts is not None:
                        elapsed = self.clock() - self.trace.tool_dispatch_ts
                    cached = bool(isinstance(result, Mapping) and result.get("cached"))
                    self.trace.fast_path = cached or elapsed < FAST_PATH_S
                    await self.speaker.finish_current_word()
                    self.trace.finished_word_before_result = True
                    self._pending.clear()
                    if _is_rate_limit_payload(result) and self.trace.retries < 1:
                        self.trace.abandoned_on_429 = True
                        self.trace.retries += 1
                        retry = await factory()
                        result = retry
                    self.trace.path = classify_dispatch_path(result)
                    if _is_rate_limit_payload(result):
                        self.trace.abandoned_on_429 = True
                        self.trace.path = "ERROR"
                        fallback = await self._error_fallback()
                        return fallback, self.trace
                    return result, self.trace

                if not self._pending:
                    await self._sleep(0.01)
                    continue

                due, stage = self._pending[0]
                now = self.clock()
                if now < due:
                    remaining = due - now
                    sleep_task = asyncio.create_task(self._sleep(remaining))
                    await asyncio.wait(
                        {tool_task, sleep_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not sleep_task.done():
                        sleep_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await sleep_task
                    continue

                self._pending.pop(0)
                if stage == 5:
                    await self.speak_stage(5)
                    fallback = await self._take_message_fallback()
                    if not tool_task.done():
                        tool_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await tool_task
                    return fallback, self.trace
                await self.speak_stage(stage)
        finally:
            self.active = False
            self._pending.clear()
