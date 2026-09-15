"""Never-silent session config, ambient gain, backchannels, agent wiring."""

from __future__ import annotations

import inspect
import random

from ambient import (
    AMBIENT_DBFS,
    AMBIENT_DBFS_MAX,
    AMBIENT_DBFS_MIN,
    ambient_volume,
    dbfs_to_linear,
)
from ava_receptionist import AVA_SPEECH_SPEED, AVA_TEMPERATURE, AVA_VAD_SILENCE_MS
from backchannel import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    PAIN_INTERVAL_S,
    BackchannelScheduler,
)
from call_state import CallState
from filler_ladder import _remaining_to_word_boundary


def test_session_config_speech_and_vad() -> None:
    assert AVA_SPEECH_SPEED == 0.9
    assert 0.9 <= AVA_TEMPERATURE <= 1.0
    assert 450 <= AVA_VAD_SILENCE_MS <= 550


def test_ambient_bed_is_minus_45_to_50_dbfs() -> None:
    assert AMBIENT_DBFS_MIN <= AMBIENT_DBFS <= AMBIENT_DBFS_MAX
    volume = ambient_volume()
    assert dbfs_to_linear(-50) <= volume <= dbfs_to_linear(-45)
    assert volume < 0.01  # quiet office, not a hold jingle


def test_backchannel_rate_limit() -> None:
    clock = {"t": 0.0}

    class Four(random.Random):
        def uniform(self, a: float, b: float) -> float:
            del b
            return a

    state = CallState()
    sched = BackchannelScheduler(state, clock=lambda: clock["t"], rng=Four(0))
    assert MIN_INTERVAL_S == 4.0
    assert MAX_INTERVAL_S == 6.0
    assert PAIN_INTERVAL_S < MIN_INTERVAL_S
    assert sched.on_user_state(True) is None
    clock["t"] = 1.0
    assert sched.maybe_fire(clock["t"]) is None
    clock["t"] = 4.1
    line = sched.maybe_fire(clock["t"])
    assert line is not None
    clock["t"] = 5.0
    assert sched.maybe_fire(clock["t"]) is None
    clock["t"] = 8.2
    again = sched.maybe_fire(clock["t"])
    assert again is not None
    assert len(sched.fired) == 2


def test_pain_backchannels_fire_sooner() -> None:
    clock = {"t": 0.0}
    state = CallState()
    state.urgency_level = "same_day"
    sched = BackchannelScheduler(
        state, clock=lambda: clock["t"], rng=__import__("random").Random(1)
    )
    sched.on_user_state(True, pain=True)
    clock["t"] = 2.6
    line = sched.maybe_fire(clock["t"], pain=True)
    assert line is not None


def test_finish_current_word_waits_out_the_word() -> None:
    remaining = _remaining_to_word_boundary(0.2, 0.45)
    assert 0 < remaining <= 0.45
    assert _remaining_to_word_boundary(0.45, 0.45) == 0


def test_agent_wires_never_silent_machinery() -> None:
    import agent
    import ava_receptionist

    agent_src = inspect.getsource(agent.my_agent)
    assert "CachedBookingProvider" in agent_src
    assert "prewarm" in agent_src
    assert "AmbientBed" in agent_src
    assert "attach_backchannels" in agent_src
    assert "mark_interrupted" in agent_src
    recv_src = inspect.getsource(ava_receptionist.AvaReceptionist)
    assert "_dispatch_with_ladder" in recv_src
    assert "play_keyboard" in recv_src
    assert "on_caller_speech" in recv_src
