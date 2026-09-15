"""Side-channel backchannels while the caller talks.

Rate limit: one per 4-6s of caller speech; denser when they describe pain.
Does not take the turn - plays on the ambient track when possible.

Docs: https://docs.livekit.io/agents/logic/turns/
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Any

from call_state import CallState, classify_urgency
from phrase_pools import BACKCHANNELS, PAIN_BACKCHANNELS

logger = logging.getLogger("ava.backchannel")

MIN_INTERVAL_S = 4.0
MAX_INTERVAL_S = 6.0
PAIN_INTERVAL_S = 2.5


class BackchannelScheduler:
    def __init__(
        self,
        state: CallState,
        *,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
        player: Any | None = None,
    ) -> None:
        self.state = state
        self.clock = clock
        self.rng = rng or random.Random()
        self.player = player
        self.last_fired_at: float | None = None
        self.fired: list[str] = []
        self.speaking = False
        self.speech_started_at: float | None = None

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock())
        import time

        return time.perf_counter()

    def interval_s(self, *, pain: bool) -> float:
        if pain:
            return PAIN_INTERVAL_S
        return self.rng.uniform(MIN_INTERVAL_S, MAX_INTERVAL_S)

    def on_user_state(self, speaking: bool, *, pain: bool = False) -> str | None:
        now = self._now()
        if speaking and not self.speaking:
            self.speaking = True
            self.speech_started_at = now
            return None
        if not speaking:
            self.speaking = False
            self.speech_started_at = None
            return None
        return self.maybe_fire(now, pain=pain)

    def maybe_fire(self, now: float, *, pain: bool = False) -> str | None:
        if not self.speaking:
            return None
        interval = self.interval_s(pain=pain)
        if self.last_fired_at is not None and (now - self.last_fired_at) < interval:
            return None
        started = self.speech_started_at if self.speech_started_at is not None else now
        if (now - started) < interval and self.last_fired_at is None:
            return None
        pool = PAIN_BACKCHANNELS if pain else BACKCHANNELS
        line = self.state.pick_phrase(
            "pain_backchannel" if pain else "backchannel", pool, rng=self.rng
        )
        self.last_fired_at = now
        self.fired.append(line)
        self.state.record_stock_phrase(line)
        return line

    def observe_transcript(self, text: str) -> bool:
        return classify_urgency(text) in {"same_day", "emergency_000"}


def attach_backchannels(
    session: Any,
    state: CallState,
    *,
    scheduler: BackchannelScheduler | None = None,
    speaker: Any | None = None,
    enabled: bool = False,
    filler_player: Any | None = None,
) -> BackchannelScheduler:
    """Attach the listener. Playback stays off until single-voice is proven.

    Overlapping mm/yep on the live Realtime mouth is a second talker.
    """
    sched = scheduler or BackchannelScheduler(state)

    def _on_user_state(ev: Any) -> None:
        if not enabled:
            return
        player = filler_player or speaker
        if player is not None and (
            getattr(player, "model_speaking", False)
            or getattr(player, "is_playing", False)
        ):
            return
        new_state = getattr(ev, "new_state", None)
        speaking = (
            str(new_state) in {"speaking", "UserState.SPEAKING"}
            or new_state == "speaking"
        )
        pain = state.urgency_level in {"same_day", "emergency_000"}
        line = sched.on_user_state(bool(speaking), pain=pain)
        if not line:
            return
        _play_side_channel(session, speaker, line)

    try:
        session.on("user_state_changed")(_on_user_state)
    except Exception:
        logger.exception("could not attach backchannel listener")
    return sched


def _play_side_channel(session: Any, speaker: Any, line: str) -> None:
    """Play without taking the turn. Prefer a side-channel speaker; never generate_reply."""
    try:
        if speaker is not None and hasattr(speaker, "play_backchannel"):
            speaker.play_backchannel(line)
            return
        say = getattr(session, "say", None)
        if callable(say):
            say(line, add_to_chat_ctx=False, allow_interruptions=True)
            return
    except TypeError:
        try:
            session.say(line)
        except Exception:
            logger.exception("backchannel say failed")
    except Exception:
        logger.exception("backchannel side-channel failed")
