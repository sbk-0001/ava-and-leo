"""DEAD_AIR detector: outbound silence while a tool or reply is in flight.

If the outbound mix has no audio for more than 900ms while a tool is pending
or a response is generating, log DEAD_AIR with the call id and what was in
flight, and publish to the desk.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from desk_events import schedule_desk_publish

logger = logging.getLogger("ava.dead_air")

DEAD_AIR_S = 0.900


def dead_air_packet(
    *,
    call_id: str | None,
    in_flight: str,
    silence_s: float,
    branch: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "call_id": call_id or "unknown",
        "in_flight": in_flight,
        "silence_s": round(silence_s, 3),
        "ok": False,
    }
    if branch:
        payload["branch"] = branch
    return {
        "type": "dead_air",
        "action": "dead_air",
        "label": "DEAD_AIR",
        "payload": payload,
        "refresh_diary": False,
    }


class DeadAirMonitor:
    def __init__(
        self,
        *,
        call_id: str | None = None,
        branch: str | None = None,
        clock: Callable[[], float] | None = None,
        threshold_s: float = DEAD_AIR_S,
        room: Any | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.call_id = call_id
        self.branch = branch
        self._clock = clock
        self.threshold_s = threshold_s
        self.room = room
        self.on_event = on_event
        self.tool_pending = False
        self.response_generating = False
        self.in_flight: str | None = None
        self.last_audio_ts: float | None = None
        self.events: list[dict[str, Any]] = []
        self._armed_key: str | None = None

    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        import time

        return time.perf_counter()

    def note_audio(self) -> None:
        self.last_audio_ts = self._now()
        self._armed_key = None

    def set_in_flight(self, what: str | None, *, pending: bool = True) -> None:
        self.in_flight = what
        self.tool_pending = bool(pending and what)
        if pending:
            if self.last_audio_ts is None:
                self.last_audio_ts = self._now()
        else:
            self.tool_pending = False
            if not self.response_generating:
                self.in_flight = None

    def set_generating(
        self, generating: bool, *, what: str = "response_generating"
    ) -> None:
        self.response_generating = generating
        if generating:
            self.in_flight = what
            if self.last_audio_ts is None:
                self.last_audio_ts = self._now()
        elif not self.tool_pending:
            self.in_flight = None

    @property
    def busy(self) -> bool:
        return self.tool_pending or self.response_generating

    def check(self) -> dict[str, Any] | None:
        if not self.busy or self.last_audio_ts is None:
            return None
        silence = self._now() - self.last_audio_ts
        if silence < self.threshold_s:
            return None
        key = f"{self.in_flight}:{round(self.last_audio_ts, 3)}"
        if self._armed_key == key:
            return None
        self._armed_key = key
        in_flight = self.in_flight or "unknown"
        packet = dead_air_packet(
            call_id=self.call_id,
            in_flight=in_flight,
            silence_s=silence,
            branch=self.branch,
        )
        self.events.append(packet)
        logger.error(
            "DEAD_AIR call_id=%s in_flight=%s silence_s=%.3f",
            self.call_id or "unknown",
            in_flight,
            silence,
        )
        if self.on_event is not None:
            self.on_event(packet)
        else:
            schedule_desk_publish(self.room, packet)
        return packet
