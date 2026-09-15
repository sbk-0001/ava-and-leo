"""14-day availability cache: cache-first, age, live miss, re-verify on book."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from availability_cache import (
    CACHE_WINDOW_DAYS,
    REFRESH_MAX_S,
    REFRESH_MIN_S,
    CachedBookingProvider,
    date_from_slot_id,
    refresh_delay_s,
)
from booking import MemoryBookingProvider
from practice import PracticeClient, seed_mock_diary


class CountingProvider:
    def __init__(self, inner: MemoryBookingProvider) -> None:
        self.inner = inner
        self.checks = 0
        self.books = 0
        self.name = "counting"

    async def check_availability(self, **kwargs):
        self.checks += 1
        return await self.inner.check_availability(**kwargs)

    async def book_appointment(self, **kwargs):
        self.books += 1
        return await self.inner.book_appointment(**kwargs)

    async def reschedule_appointment(self, **kwargs):
        return await self.inner.reschedule_appointment(**kwargs)

    async def cancel_appointment(self, **kwargs):
        return await self.inner.cancel_appointment(**kwargs)

    async def lookup_patient(self, **kwargs):
        return await self.inner.lookup_patient(**kwargs)

    async def take_message(self, **kwargs):
        return await self.inner.take_message(**kwargs)


def _cache(
    today: date = date(2026, 9, 15),
) -> tuple[CachedBookingProvider, CountingProvider]:
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=today, days=14)
    inner = CountingProvider(MemoryBookingProvider(client))
    cache = CachedBookingProvider(
        inner,  # type: ignore[arg-type]
        today_fn=lambda: today,
        clock=lambda: 1000.0,
    )
    cache.inner = inner  # counting is the live source
    return cache, inner


def test_refresh_interval_is_two_to_three_minutes() -> None:
    import random

    rng = random.Random(0)
    delays = [refresh_delay_s(rng) for _ in range(40)]
    assert all(REFRESH_MIN_S <= d <= REFRESH_MAX_S for d in delays)
    assert min(delays) < 150
    assert max(delays) > 150


def test_date_from_slot_id() -> None:
    assert (
        date_from_slot_id("slot_shellharbour_2026-09-15_0800_dr-mohit-tolani")
        == "2026-09-15"
    )
    assert date_from_slot_id("nope") is None


@pytest.mark.asyncio
async def test_cache_first_skips_live_and_marks_age() -> None:
    cache, inner = _cache()
    await cache.prewarm(("shellharbour",))
    assert inner.checks == 1
    import time

    t0 = time.perf_counter()
    result = await cache.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert inner.checks == 1
    assert result["cached"] is True
    assert result["ok"] is True
    assert result["slots"]
    assert result["cache_age_s"] is not None
    assert result["cache_fetched_at"]
    assert elapsed_ms < 50
    assert cache.cache_hits == 1
    assert cache.window_for("shellharbour") is not None
    window = cache.window_for("shellharbour")
    assert window is not None
    start = date.fromisoformat(window.date_from)
    end = date.fromisoformat(window.date_to)
    assert (end - start).days == CACHE_WINDOW_DAYS - 1


@pytest.mark.asyncio
async def test_outside_window_hits_live() -> None:
    cache, inner = _cache()
    await cache.prewarm(("shellharbour",))
    far = (date(2026, 9, 15) + timedelta(days=30)).isoformat()
    result = await cache.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range=far,
    )
    assert result["cached"] is False
    assert inner.checks == 2


@pytest.mark.asyncio
async def test_book_reverify_live_before_commit() -> None:
    cache, inner = _cache()
    await cache.prewarm(("shellharbour",))
    slots = await cache.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    slot_id = slots["slots"][0]["slot_id"]
    checks_before_book = inner.checks
    booked = await cache.book_appointment(
        branch="shellharbour",
        slot_id=slot_id,
        reason="check-up",
        name="Sam Nguyen",
        mobile="0412334556",
    )
    assert booked["confirmed"] is True
    assert booked["reverified"] is True
    assert inner.checks == checks_before_book + 1
    assert inner.books == 1
    assert cache.reverifies == 1


@pytest.mark.asyncio
async def test_book_slot_gone_does_not_confirm() -> None:
    cache, inner = _cache()
    await cache.prewarm(("shellharbour",))
    slots = await cache.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    slot_id = slots["slots"][0]["slot_id"]
    live_slot = inner.inner.client.slots[slot_id]
    live_slot.taken = True
    booked = await cache.book_appointment(
        branch="shellharbour",
        slot_id=slot_id,
        reason="check-up",
        name="Sam",
        mobile="0412334556",
    )
    assert booked["ok"] is False
    assert booked["confirmed"] is False
    assert booked["reason"] == "slot_gone"
    assert booked["reverified"] is True
    assert inner.books == 0
