"""DEAD_AIR detector: 900ms outbound silence while a tool or reply is in flight."""

from __future__ import annotations

from dead_air import DEAD_AIR_S, DeadAirMonitor, dead_air_packet


def test_dead_air_threshold_is_900ms() -> None:
    assert DEAD_AIR_S == 0.900


def test_dead_air_fires_when_tool_pending_and_silent() -> None:
    clock = {"t": 0.0}
    published: list[dict] = []
    monitor = DeadAirMonitor(
        call_id="AJ_test",
        branch="shellharbour",
        clock=lambda: clock["t"],
        on_event=published.append,
    )
    monitor.set_in_flight("check_availability", pending=True)
    monitor.note_audio()
    clock["t"] = 0.5
    assert monitor.check() is None
    clock["t"] = 1.5
    event = monitor.check()
    assert event is not None
    assert event["label"] == "DEAD_AIR"
    assert event["payload"]["call_id"] == "AJ_test"
    assert event["payload"]["in_flight"] == "check_availability"
    assert event["payload"]["silence_s"] >= 0.9
    assert published == [event]
    clock["t"] = 1.6
    assert monitor.check() is None


def test_dead_air_packet_shape() -> None:
    packet = dead_air_packet(
        call_id="c1", in_flight="response_generating", silence_s=1.2
    )
    assert packet["type"] == "dead_air"
    assert packet["payload"]["in_flight"] == "response_generating"


def test_audio_resets_the_timer() -> None:
    clock = {"t": 0.0}
    monitor = DeadAirMonitor(clock=lambda: clock["t"], on_event=lambda *_: None)
    monitor.set_generating(True)
    clock["t"] = 0.8
    monitor.note_audio()
    clock["t"] = 1.5
    assert monitor.check() is None
    clock["t"] = 2.5
    assert monitor.check() is not None
