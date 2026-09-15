"""Play pre-rendered filler PCM. No model, no TTS, no session.say.

Live path mixes into the ambient BackgroundAudioPlayer (same heard mix as
the office bed). If model audio starts while a filler plays, duck over 80ms
— never hard-cut. First audio is a buffer read, so stage-1 can precede the
tool network call.

Docs: https://docs.livekit.io/agents/multimodality/audio/background-audio.md
      https://docs.livekit.io/agents/multimodality/audio/customization.md
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from array import array
from collections.abc import AsyncIterator, Callable
from typing import Any

from filler_bank import SAMPLE_RATE, FillerBank, FillerClip, get_filler_bank

logger = logging.getLogger("ava.filler_player")

DUCK_S = 0.080
FRAME_SAMPLES = 960  # 20ms at 48kHz


def duck_gain(elapsed_s: float, *, duck_s: float = DUCK_S) -> float:
    """Linear fade 1→0 over duck_s. 0 elapsed keeps full gain (never a hard cut)."""
    if elapsed_s <= 0:
        return 1.0
    if duck_s <= 0 or elapsed_s >= duck_s:
        return 0.0
    return 1.0 - (elapsed_s / duck_s)


def pcm_frames(
    pcm: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    samples_per_frame: int = FRAME_SAMPLES,
) -> list[bytes]:
    width = 2
    frame_bytes = samples_per_frame * width
    frames = [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]
    return [frame for frame in frames if frame]


def apply_gain(pcm: bytes, gain: float) -> bytes:
    if gain >= 0.999:
        return pcm
    if gain <= 0.0:
        return b"\x00" * len(pcm)
    samples = array("h")
    samples.frombytes(pcm)
    scaled = array(
        "h", (int(max(-32767, min(32767, sample * gain))) for sample in samples)
    )
    return scaled.tobytes()


class FillerPlayer:
    """Buffer-read filler playback with 80ms ducking against model audio."""

    def __init__(
        self,
        bank: FillerBank | None = None,
        *,
        session: Any | None = None,
        ambient: Any | None = None,
        clock: Callable[[], float] | None = None,
        on_audio: Callable[[], None] | None = None,
    ) -> None:
        self.bank = bank
        self.session = session
        self.ambient = ambient
        self.clock = clock or time.perf_counter
        self.on_audio = on_audio
        self.last_first_audio_ts: float | None = None
        self.last_text: str | None = None
        self.started_at: float | None = None
        self.played: list[str] = []
        self.played_pcm: list[bytes] = []
        self.gains: list[float] = []
        self._model_audio_at: float | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._live_handle: Any | None = None

    def _bank(self) -> FillerBank:
        return self.bank or get_filler_bank()

    def notify_model_audio(self) -> None:
        """Model speech started. Duck any in-flight filler over ~80ms."""
        if self._model_audio_at is None:
            self._model_audio_at = self.clock()
        handle = self._live_handle
        if handle is not None:
            stopper = getattr(handle, "stop", None)
            if callable(stopper):
                try:
                    stopper()
                except Exception:
                    logger.exception("filler duck stop failed")

    def reset_model_audio(self) -> None:
        self._model_audio_at = None
        self._stop = asyncio.Event()

    def _note_audio(self) -> None:
        if self.last_first_audio_ts is None:
            self.last_first_audio_ts = self.clock()
        if self.on_audio is not None:
            self.on_audio()

    async def play(self, text: str, *, allow_interruptions: bool = True) -> FillerClip:
        del allow_interruptions
        clip = self._bank().get(text)
        self.last_text = text
        self.started_at = self.clock()
        self.last_first_audio_ts = None
        self._model_audio_at = None
        self._stop = asyncio.Event()
        self.played.append(text)
        await self._start_playout(clip)
        self._note_audio()
        return clip

    async def play_pool(self, pool: str, text: str | None = None) -> FillerClip:
        if text is None:
            clips = self._bank().pool_clips(pool)
            if not clips:
                raise RuntimeError(f"empty filler pool {pool!r}")
            text = clips[0].text
        return await self.play(text)

    async def _start_playout(self, clip: FillerClip) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        live = await self._try_live_play(clip)
        # Always mix PCM onto the session output as well so tool waits are
        # never silent if BackgroundAudioPlayer isn't started yet.
        self._task = asyncio.create_task(
            self._mix_playout(clip, skip_capture=live),
            name="ava-filler-playout",
        )
        await asyncio.sleep(0)

    async def _try_live_play(self, clip: FillerClip) -> bool:
        """Play the WAV via BackgroundAudioPlayer when the office bed is up.

        Path playback uses LiveKit's optimized WAV decoder. fade_out=80ms so
        handle.stop() ducks instead of hard-cutting when model audio starts.
        Docs: https://docs.livekit.io/agents/multimodality/audio/background-audio.md
        """
        player = None
        if self.ambient is not None:
            player = getattr(self.ambient, "player", None)
        play = getattr(player, "play", None) if player is not None else None
        if not callable(play):
            return False
        try:
            from livekit.agents import AudioConfig
        except Exception:
            return False

        source: Any = str(clip.path) if clip.path.is_file() else self._frame_iter(clip)

        try:
            kwargs: dict[str, Any] = {"volume": 1.0}
            sig = inspect.signature(AudioConfig)
            if "fade_out" in sig.parameters:
                kwargs["fade_out"] = DUCK_S
            handle = play(AudioConfig(source, **kwargs))
            if inspect.isawaitable(handle):
                handle = await handle
            self._live_handle = handle
            return True
        except Exception:
            logger.exception("live filler play failed; using local mix")
            return False

    def _frame_iter(self, clip: FillerClip) -> AsyncIterator[Any]:
        async def _frames() -> AsyncIterator[Any]:
            try:
                from livekit import rtc
            except Exception:
                return
                yield  # pragma: no cover
            t0 = self.clock()
            for chunk in pcm_frames(clip.pcm, sample_rate=clip.sample_rate):
                elapsed_model = 0.0
                if self._model_audio_at is not None:
                    elapsed_model = self.clock() - self._model_audio_at
                gain = duck_gain(elapsed_model)
                self.gains.append(gain)
                data = apply_gain(chunk, gain)
                samples = max(1, len(data) // 2)
                yield rtc.AudioFrame(
                    data=data,
                    sample_rate=clip.sample_rate,
                    num_channels=1,
                    samples_per_channel=samples,
                )
                self._note_audio()
                if gain <= 0.0 and self._model_audio_at is not None:
                    break
                took = self.clock() - t0
                expected = samples / float(clip.sample_rate)
                delay = expected - took
                t0 = self.clock()
                if delay > 0:
                    await asyncio.sleep(delay)

        return _frames()

    async def _capture_chunk(self, data: bytes, sample_rate: int) -> None:
        sink = getattr(self.session, "output", None)
        audio_out = getattr(sink, "audio", None) if sink is not None else None
        capture = getattr(audio_out, "capture_frame", None)
        if not callable(capture):
            return
        try:
            from livekit import rtc

            samples = max(1, len(data) // 2)
            frame = rtc.AudioFrame(
                data=data,
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=samples,
            )
            result = capture(frame)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("filler capture_frame failed")

    async def _mix_playout(
        self, clip: FillerClip, *, skip_capture: bool = False
    ) -> None:
        mixed = bytearray()
        samples_since_model = 0
        for chunk in pcm_frames(clip.pcm, sample_rate=clip.sample_rate):
            elapsed_model = 0.0
            if self._model_audio_at is not None:
                elapsed_wall = self.clock() - self._model_audio_at
                elapsed_samples = samples_since_model / float(clip.sample_rate)
                elapsed_model = max(elapsed_wall, elapsed_samples)
                samples_since_model += max(1, len(chunk) // 2)
            gain = duck_gain(elapsed_model)
            self.gains.append(gain)
            faded = apply_gain(chunk, gain)
            mixed.extend(faded)
            self._note_audio()
            if not skip_capture:
                await self._capture_chunk(faded, clip.sample_rate)
            if gain <= 0.0 and self._model_audio_at is not None:
                break
            if self._stop.is_set():
                break
            await asyncio.sleep(0)
        self.played_pcm.append(bytes(mixed) or clip.pcm)

    async def wait_for_playout(self) -> None:
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                return
            return
        handle = self._live_handle
        wait = getattr(handle, "wait_for_playout", None)
        if callable(wait):
            result = wait()
            if inspect.isawaitable(result):
                await result
