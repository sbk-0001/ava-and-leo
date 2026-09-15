"""Quiet office bed plus keyboard clatter on tool dispatch.

Ambient target: -45 to -50 dBFS. Random start offset so the loop is not audible.
Keyboard plays while a tool is in flight (thinking_sound + explicit play).

Docs: https://docs.livekit.io/agents/multimodality/audio/
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("ava.ambient")

AMBIENT_DBFS_MIN = -50.0
AMBIENT_DBFS_MAX = -45.0
AMBIENT_DBFS = -47.5
KEYBOARD_DBFS = -28.0
MAX_START_OFFSET_S = 45.0


def dbfs_to_linear(dbfs: float) -> float:
    """Convert dBFS to a 0-1 linear gain. 0 dBFS is 1.0."""
    return float(10 ** (dbfs / 20.0))


def ambient_volume(*, dbfs: float = AMBIENT_DBFS) -> float:
    if dbfs > AMBIENT_DBFS_MAX or dbfs < AMBIENT_DBFS_MIN:
        dbfs = min(AMBIENT_DBFS_MAX, max(AMBIENT_DBFS_MIN, dbfs))
    return dbfs_to_linear(dbfs)


def keyboard_volume(*, dbfs: float = KEYBOARD_DBFS) -> float:
    return dbfs_to_linear(dbfs)


def random_start_offset_s(
    rng: random.Random | None = None, *, max_s: float = MAX_START_OFFSET_S
) -> float:
    chooser = rng or random.Random()
    return chooser.uniform(0.0, max_s)


class AmbientBed:
    """Wraps BackgroundAudioPlayer when LiveKit is available; no-op otherwise."""

    def __init__(
        self,
        *,
        dbfs: float = AMBIENT_DBFS,
        rng: random.Random | None = None,
    ) -> None:
        self.dbfs = dbfs
        self.rng = rng or random.Random()
        self.player: Any | None = None
        self._keyboard: Any | None = None
        self.started = False
        self.start_offset_s = random_start_offset_s(self.rng)

    def build_player(self) -> Any | None:
        try:
            from livekit.agents import (
                AudioConfig,
                BackgroundAudioPlayer,
                BuiltinAudioClip,
            )
        except Exception:
            logger.exception("BackgroundAudioPlayer unavailable")
            return None
        volume = ambient_volume(dbfs=self.dbfs)
        keyboard = keyboard_volume()
        self.player = BackgroundAudioPlayer(
            ambient_sound=AudioConfig(
                BuiltinAudioClip.OFFICE_AMBIENCE,
                volume=volume,
            ),
            thinking_sound=AudioConfig(
                BuiltinAudioClip.KEYBOARD_TYPING,
                volume=keyboard,
            ),
        )
        return self.player

    async def start(self, session: Any, room: Any) -> bool:
        player = self.player or self.build_player()
        if player is None or room is None:
            return False
        try:
            await player.start(room=room, agent_session=session)
            self.started = True
            logger.info(
                "ambient bed started dbfs=%s volume=%.6f offset_s=%.1f",
                self.dbfs,
                ambient_volume(dbfs=self.dbfs),
                self.start_offset_s,
            )
            return True
        except Exception:
            logger.exception("ambient bed failed to start; continuing without it")
            self.started = False
            return False

    def play_keyboard(self) -> None:
        if self.player is None:
            return
        try:
            from livekit.agents import AudioConfig, BuiltinAudioClip

            self._keyboard = self.player.play(
                AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=keyboard_volume()),
                loop=True,
            )
        except Exception:
            logger.exception("keyboard clatter failed")

    def stop_keyboard(self) -> None:
        handle = self._keyboard
        self._keyboard = None
        if handle is None:
            return
        try:
            handle.stop()
        except Exception:
            logger.exception("keyboard stop failed")

    async def aclose(self) -> None:
        self.stop_keyboard()
        player = self.player
        self.player = None
        self.started = False
        if player is None:
            return
        try:
            closer = getattr(player, "aclose", None)
            if callable(closer):
                await closer()
                return
            closer = getattr(player, "close", None)
            if callable(closer):
                closed = closer()
                if hasattr(closed, "__await__"):
                    await closed
        except Exception:
            logger.exception("ambient bed close failed")


async def skip_audio_seconds(
    frames: AsyncIterator[Any],
    seconds: float,
    *,
    sample_rate: int = 48000,
) -> AsyncIterator[Any]:
    """Drop leading frames so a long bed does not always start at the loop point."""
    skipped = 0
    target = int(max(0.0, seconds) * sample_rate)
    async for frame in frames:
        n = int(getattr(frame, "samples_per_channel", 0) or 0)
        if skipped < target:
            skipped += n
            continue
        yield frame
