"""BookingProvider: memory seeds real slots; Zavy360 stub never invents them."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking import (
    MemoryBookingProvider,
    TimeoutBookingProvider,
    Zavy360BookingProvider,
    cancellation_fee_applies,
    parse_date_range,
)
from practice import PracticeClient, seed_mock_diary

SYDNEY = ZoneInfo("Australia/Sydney")


def _client() -> PracticeClient:
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=date(2026, 9, 15), days=7)
    return client


@pytest.mark.asyncio
async def test_memory_provider_has_slots_for_all_three_branches() -> None:
    provider = MemoryBookingProvider(_client())
    for branch in ("shellharbour", "dapto", "woonona"):
        result = await provider.check_availability(
            branch=branch,
            appointment_type="check-up",
            date_range="this week",
        )
        assert result["ok"] is True
        assert result["slots"], branch
        assert all(slot["branch_id"] == branch for slot in result["slots"])


@pytest.mark.asyncio
async def test_memory_book_and_cancel_inside_24h_applies_fee() -> None:
    client = PracticeClient(mode="mock")
    today = date(2026, 9, 15)
    client.seed_patient(patient_id="p1", name="Priya Nair", phone="0413000222")
    client.seed_slot(
        slot_id="slot-soon",
        branch_id="shellharbour",
        date=today.isoformat(),
        time="16:00",
        clinician="Dr Mohit Tolani",
    )
    provider = MemoryBookingProvider(client)
    booked = await provider.book_appointment(
        branch="shellharbour",
        slot_id="slot-soon",
        reason="check-up",
        patient_id="p1",
        name="Priya Nair",
        mobile="0413000222",
    )
    assert booked["confirmed"] is True
    cancelled = await provider.cancel_appointment(booking_id=booked["booking_id"])
    assert cancelled["confirmed"] is True
    assert cancelled["fee_applies"] is True
    assert cancelled["fee_aud"] == 50


def test_cancellation_fee_window() -> None:
    now = datetime(2026, 9, 16, 9, 0, tzinfo=SYDNEY)
    assert cancellation_fee_applies("2026-09-16", "15:00", now=now) is True
    later = (now + timedelta(hours=48)).date().isoformat()
    assert cancellation_fee_applies(later, "10:00", now=now) is False


@pytest.mark.asyncio
async def test_lookup_patient_by_mobile() -> None:
    client = _client()
    provider = MemoryBookingProvider(client)
    found = await provider.lookup_patient(mobile="0413000222")
    assert found["ok"] is True
    assert found["is_existing_patient"] is True
    missing = await provider.lookup_patient(mobile="0499999999")
    assert missing["is_existing_patient"] is False
    assert missing["patients"] == []


@pytest.mark.asyncio
async def test_zavy360_stub_does_not_invent_slots() -> None:
    stub = Zavy360BookingProvider()
    result = await stub.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    assert result["ok"] is False
    assert "zavy360" in result["reason"]
    assert result.get("slots") in (None, [])
    assert result.get("confirmed") is not True


@pytest.mark.asyncio
async def test_timeout_wrapper_recovers_without_fake_slots() -> None:
    inner = MemoryBookingProvider(_client())
    wrapped = TimeoutBookingProvider(inner, force_timeout=True)
    result = await wrapped.book_appointment(
        branch="shellharbour",
        slot_id="nope",
        reason="check-up",
        name="Sam",
        mobile="0412000111",
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert "invent" in result["note"].lower()


def test_parse_date_range() -> None:
    today = date(2026, 9, 15)
    assert parse_date_range("2026-09-22", today=today) == ("2026-09-22", "2026-09-22")
    start, end = parse_date_range("this week", today=today)
    assert start == "2026-09-15"
    assert end == "2026-09-21"
