"""14-day per-branch availability cache. check_availability is cache-first.

Background refresh every 2-3 minutes. Live source only on miss or outside the
window. Bookings re-verify live before commit.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from booking import (
    BookingProvider,
    filter_slots_by_clinician,
    invalid_slot_id_result,
    is_canonical_slot_id,
    parse_date_range,
    stamp_availability_status,
)
from persona import BRANCHES

logger = logging.getLogger("ava.cache")

SYDNEY = ZoneInfo("Australia/Sydney")
CACHE_WINDOW_DAYS = 14
AGENT_SLOT_LIMIT = 12
REFRESH_MIN_S = 120.0
REFRESH_MAX_S = 180.0
_SLOT_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _sydney_today() -> date:
    return datetime.now(SYDNEY).date()


def date_from_slot_id(slot_id: str) -> str | None:
    match = _SLOT_DATE_RE.search(slot_id or "")
    return match.group(1) if match else None


def refresh_delay_s(rng: random.Random | None = None) -> float:
    chooser = rng or random.Random()
    return chooser.uniform(REFRESH_MIN_S, REFRESH_MAX_S)


@dataclass
class BranchWindow:
    branch_id: str
    date_from: str
    date_to: str
    slots: list[dict[str, Any]]
    fetched_at: float
    fetched_at_iso: str

    def covers(self, start: str, end: str) -> bool:
        return self.date_from <= start and end <= self.date_to

    def age_s(self, now: float | None = None) -> float:
        return max(
            0.0, (now if now is not None else time.perf_counter()) - self.fetched_at
        )

    def slots_in_range(self, start: str, end: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for slot in self.slots:
            day = str(slot.get("date") or "")
            if start <= day <= end and not slot.get("taken"):
                out.append(slot)
        return out


@dataclass
class CachedBookingProvider:
    """BookingProvider wrapper. Portal still talks to PracticeClient directly."""

    inner: BookingProvider
    window_days: int = CACHE_WINDOW_DAYS
    branches: Sequence[str] = field(default_factory=lambda: tuple(BRANCHES))
    clock: Callable[[], float] = time.perf_counter
    today_fn: Callable[[], date] = _sydney_today
    rng: random.Random = field(default_factory=random.Random)
    _windows: dict[str, BranchWindow] = field(default_factory=dict)
    live_checks: int = 0
    cache_hits: int = 0
    reverifies: int = 0

    @property
    def name(self) -> str:
        return f"cached:{getattr(self.inner, 'name', 'booking')}"

    def window_for(self, branch: str) -> BranchWindow | None:
        return self._windows.get(branch)

    def _today_range(self) -> tuple[str, str]:
        start = self.today_fn()
        end = start + timedelta(days=self.window_days - 1)
        return start.isoformat(), end.isoformat()

    async def prewarm(self, branches: Sequence[str] | None = None) -> None:
        for branch_id in branches or self.branches:
            try:
                await self.refresh_branch(branch_id)
            except Exception:
                logger.exception("availability prewarm failed branch=%s", branch_id)

    async def refresh_branch(self, branch: str) -> BranchWindow | None:
        date_from, date_to = self._today_range()
        self.live_checks += 1
        result = await self.inner.check_availability(
            branch=branch,
            appointment_type="any",
            date_range=f"{date_from}/{date_to}",
            clinician=None,
            limit=None,
        )
        if not result.get("ok"):
            logger.warning(
                "skip caching failed availability branch=%s reason=%s",
                branch,
                result.get("reason"),
            )
            return None
        now = self.clock()
        window = BranchWindow(
            branch_id=branch,
            date_from=date_from,
            date_to=date_to,
            slots=list(result.get("slots") or []),
            fetched_at=now,
            fetched_at_iso=datetime.now(SYDNEY).isoformat(),
        )
        self._windows[branch] = window
        return window

    def next_refresh_delay(self) -> float:
        return refresh_delay_s(self.rng)

    async def run_refresh_loop(
        self,
        stop: asyncio.Event,
        *,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        sleep = sleeper or asyncio.sleep
        while not stop.is_set():
            delay = self.next_refresh_delay()
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            except Exception:
                await sleep(delay)
            if stop.is_set():
                return
            await self.prewarm()

    async def check_availability(
        self,
        *,
        branch: str,
        appointment_type: str,
        date_range: str,
        clinician: str | None = None,
        limit: int | None = AGENT_SLOT_LIMIT,
    ) -> dict[str, Any]:
        start, end = parse_date_range(date_range, today=self.today_fn())
        window = self._windows.get(branch)
        if window is None:
            await self.refresh_branch(branch)
            window = self._windows.get(branch)
        if window is not None and window.covers(start, end):
            in_range = window.slots_in_range(start, end)
            slots = filter_slots_by_clinician(in_range, clinician)
            clinician_miss = bool(
                clinician
                and not slots
                and any(str(slot.get("clinician") or "").strip() for slot in in_range)
            )
            if slots or clinician_miss:
                self.cache_hits += 1
                payload: dict[str, Any] = {
                    "ok": True,
                    "cached": True,
                    "cache_age_s": round(window.age_s(self.clock()), 3),
                    "cache_fetched_at": window.fetched_at_iso,
                    "branch_id": branch,
                    "appointment_type": appointment_type,
                    "date_from": start,
                    "date_to": end,
                    "slots": slots if limit is None else slots[:limit],
                }
                if clinician:
                    payload["clinician"] = clinician
                if clinician_miss:
                    payload["note"] = (
                        "No slots for that dentist in this range. Do not invent a time. "
                        "Offer another dentist or another day."
                    )
                return stamp_availability_status(
                    payload, requested_from=start, requested_to=end
                )

        self.live_checks += 1
        result = await self.inner.check_availability(
            branch=branch,
            appointment_type=appointment_type,
            date_range=f"{start}/{end}",
            clinician=clinician,
            limit=limit,
        )
        payload = dict(result)
        payload["cached"] = False
        payload["cache_age_s"] = None
        if payload.get("ok"):
            payload["slots"] = list(payload.get("slots") or [])[:AGENT_SLOT_LIMIT]
            return stamp_availability_status(
                payload, requested_from=start, requested_to=end
            )
        overlap: list[dict[str, Any]] = []
        covered_from = None
        covered_to = None
        if window is not None:
            overlap_start = max(start, window.date_from)
            overlap_end = min(end, window.date_to)
            if overlap_start <= overlap_end:
                overlap = filter_slots_by_clinician(
                    window.slots_in_range(overlap_start, overlap_end), clinician
                )
                covered_from = overlap_start
                covered_to = overlap_end
        if overlap:
            payload["ok"] = True
            payload["slots"] = overlap if limit is None else overlap[:limit]
            payload["cached"] = True
            return stamp_availability_status(
                payload,
                requested_from=start,
                requested_to=end,
                covered_from=covered_from,
                covered_to=covered_to,
            )
        return stamp_availability_status(
            payload, requested_from=start, requested_to=end
        )

    def _lookup_slot(self, slot_id: str) -> Any | None:
        provider: Any = self.inner
        for _ in range(4):
            client = getattr(provider, "client", None)
            slots = getattr(client, "slots", None) if client is not None else None
            if isinstance(slots, dict) and slot_id in slots:
                return slots[slot_id]
            provider = getattr(provider, "inner", None)
            if provider is None:
                break
        return None

    def _slot_still_open(
        self, *, branch: str, slot_id: str, live: Mapping[str, Any]
    ) -> bool | None:
        """True / False if known, None if the live source could not say."""
        del branch
        found = self._lookup_slot(slot_id)
        if found is not None:
            return not bool(getattr(found, "taken", False))
        if not live.get("ok"):
            return None
        for slot in live.get("slots") or []:
            if slot.get("slot_id") == slot_id:
                return not bool(slot.get("taken"))
        listed = live.get("slots")
        if listed:
            return False
        return None

    async def book_appointment(
        self,
        *,
        branch: str,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        if not is_canonical_slot_id(slot_id):
            return invalid_slot_id_result(slot_id)
        day = date_from_slot_id(slot_id) or "this week"
        self.live_checks += 1
        self.reverifies += 1
        live = await self.inner.check_availability(
            branch=branch,
            appointment_type=reason or "existing",
            date_range=day,
        )
        still = self._slot_still_open(branch=branch, slot_id=slot_id, live=live)
        if still is False:
            return {
                "ok": False,
                "confirmed": False,
                "reason": "slot_gone",
                "reverified": True,
                "note": (
                    "That time just went. Do not say confirmed. Offer another slot "
                    "or take a message."
                ),
            }
        if live.get("ok") is False and live.get("reason") == "timeout":
            payload = dict(live)
            payload["reverified"] = True
            payload["confirmed"] = False
            return payload
        result = await self.inner.book_appointment(
            branch=branch,
            slot_id=slot_id,
            reason=reason,
            patient_id=patient_id,
            name=name,
            mobile=mobile,
            date_of_birth=date_of_birth,
        )
        payload = dict(result)
        payload["reverified"] = True
        return payload

    async def reschedule_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.reschedule_appointment(**kwargs)

    async def cancel_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.cancel_appointment(**kwargs)

    async def lookup_patient(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.lookup_patient(**kwargs)

    async def take_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.take_message(**kwargs)
