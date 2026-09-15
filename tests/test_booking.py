"""BookingProvider: memory seeds real slots; Zavy360 stub never invents them."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking import (
    MemoryBookingProvider,
    TimeoutBookingProvider,
    ToolPacingProvider,
    Zavy360BookingProvider,
    apply_job_booking_overrides,
    cancellation_fee_applies,
    filter_slots_by_clinician,
    invalid_slot_id_result,
    is_canonical_slot_id,
    parse_date_range,
    seed_inside_24h_booking,
    spoken_two_slot_offer,
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


@pytest.mark.asyncio
async def test_lookup_delay_only_slows_availability() -> None:
    inner = MemoryBookingProvider(_client())
    paced = ToolPacingProvider(inner, lookup_delay_s=0.05)
    started = datetime.now(SYDNEY)
    result = await paced.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    elapsed = (datetime.now(SYDNEY) - started).total_seconds()
    assert result["ok"] is True
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_lookup_hang_does_not_return_fake_slots() -> None:
    inner = MemoryBookingProvider(_client())
    paced = ToolPacingProvider(inner, lookup_hang_s=0.05)
    result = await paced.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="this week",
    )
    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert result.get("slots") in (None, [])


def test_job_metadata_wraps_and_seeds_cancel() -> None:
    client = PracticeClient(mode="mock")
    inner = MemoryBookingProvider(client)
    wrapped = apply_job_booking_overrides(
        inner,
        {
            "booking_delay_s": 6,
            "seed_cancel_24h": True,
        },
    )
    assert isinstance(wrapped, ToolPacingProvider)
    assert wrapped.lookup_delay_s == 6
    booking = seed_inside_24h_booking(client)
    assert booking is not None
    assert cancellation_fee_applies(booking.date, booking.time) is True


def test_parse_date_range() -> None:
    today = date(2026, 9, 15)
    assert parse_date_range("2026-09-22", today=today) == ("2026-09-22", "2026-09-22")
    start, end = parse_date_range("this week", today=today)
    assert start == "2026-09-15"
    assert end == "2026-09-21"


def test_parse_date_range_next_week_from_tuesday() -> None:
    """Tue 15 Sep 2026: next week is Mon 21 - Sun 27, not this week."""
    today = date(2026, 9, 15)
    assert today.weekday() == 1  # Tuesday
    assert parse_date_range("next week", today=today) == ("2026-09-21", "2026-09-27")
    assert parse_date_range("Next Week", today=today) == ("2026-09-21", "2026-09-27")
    assert parse_date_range("for next week please", today=today) == (
        "2026-09-21",
        "2026-09-27",
    )


def test_parse_date_range_next_weekday_strictly_after_today() -> None:
    """If today is Tuesday, next Tuesday is +7, not today."""
    today = date(2026, 9, 15)
    assert parse_date_range("next tuesday", today=today) == ("2026-09-22", "2026-09-22")
    assert parse_date_range("next Tuesday", today=today) == ("2026-09-22", "2026-09-22")
    assert parse_date_range("next tue", today=today) == ("2026-09-22", "2026-09-22")
    assert parse_date_range("the next tuesday", today=today) == (
        "2026-09-22",
        "2026-09-22",
    )
    assert parse_date_range("next wednesday", today=today) == (
        "2026-09-16",
        "2026-09-16",
    )
    assert parse_date_range("next monday", today=today) == ("2026-09-21", "2026-09-21")
    assert parse_date_range("next friday", today=today) == ("2026-09-18", "2026-09-18")


def test_parse_date_range_keeps_today_tomorrow_iso() -> None:
    today = date(2026, 9, 15)
    assert parse_date_range("today", today=today) == ("2026-09-15", "2026-09-15")
    assert parse_date_range("tomorrow", today=today) == ("2026-09-16", "2026-09-16")
    assert parse_date_range("2026-09-22/2026-09-24", today=today) == (
        "2026-09-22",
        "2026-09-24",
    )
    start, end = parse_date_range("sometime soon-ish", today=today)
    assert start == "2026-09-15"
    assert end == "2026-09-21"


def test_filter_slots_by_clinician_matches_dr_mohit() -> None:
    slots = [
        {"slot_id": "a", "clinician": "Dr Mohit Tolani", "date": "2026-09-22"},
        {"slot_id": "b", "clinician": "Dr Pat Pandey", "date": "2026-09-22"},
        {"slot_id": "c", "clinician": "Dr Mohit Tolani", "date": "2026-09-23"},
    ]
    matched = filter_slots_by_clinician(slots, "Dr Mohit")
    assert [s["slot_id"] for s in matched] == ["a", "c"]
    assert filter_slots_by_clinician(slots, "mohit") == matched
    none = filter_slots_by_clinician(slots, "Dr Nobody")
    assert none == []


def test_filter_slots_by_clinician_skips_when_field_missing() -> None:
    slots = [{"slot_id": "a", "date": "2026-09-22", "time": "10:00"}]
    assert filter_slots_by_clinician(slots, "Dr Mohit") == slots


def test_spoken_two_slot_offer() -> None:
    slots = [
        {
            "date": "2026-09-22",
            "time": "10:00",
            "clinician": "Dr Mohit Tolani",
        },
        {
            "date": "2026-09-23",
            "time": "14:30",
            "clinician": "Dr Mohit Tolani",
        },
    ]
    line = spoken_two_slot_offer(slots)
    assert "Tuesday" in line
    assert "22" in line
    assert "10:00" in line or "ten" in line.lower()
    assert "Wednesday" in line
    assert "Mohit" in line
    assert "which" in line.lower() or "or" in line.lower()


@pytest.mark.asyncio
async def test_memory_provider_filters_clinician() -> None:
    provider = MemoryBookingProvider(_client())
    all_slots = await provider.check_availability(
        branch="shellharbour",
        appointment_type="root canal",
        date_range="2026-09-15/2026-09-21",
    )
    mohit = await provider.check_availability(
        branch="shellharbour",
        appointment_type="root canal",
        date_range="2026-09-15/2026-09-21",
        clinician="Dr Mohit",
    )
    assert mohit["ok"] is True
    assert mohit["slots"]
    assert all("mohit" in (s.get("clinician") or "").lower() for s in mohit["slots"])
    names = {(s.get("clinician") or "") for s in all_slots["slots"]}
    assert len(names) > 1
    assert not all("mohit" in name.lower() for name in names)
    missing = await provider.check_availability(
        branch="shellharbour",
        appointment_type="root canal",
        date_range="2026-09-15/2026-09-21",
        clinician="Dr Nobody",
    )
    assert missing["ok"] is True
    assert missing["slots"] == []
    assert "invent" in (missing.get("note") or "").lower()


def test_canonical_slot_id_rejects_invented_ids() -> None:
    assert is_canonical_slot_id("slot_shellharbour_2026-09-22_0830_dr-mohit-tolani")
    assert not is_canonical_slot_id("slot-8-30-tuesday-dr-mohit-tolani-follow-up")
    assert not is_canonical_slot_id("slot_priya_tomorrow")
    rejected = invalid_slot_id_result("slot-8-30-tuesday-dr-mohit-tolani-follow-up")
    assert rejected["reason"] == "invalid_slot_id"
    assert rejected["ok"] is False
    assert rejected["confirmed"] is False
    assert "check_availability" in rejected["note"]


@pytest.mark.asyncio
async def test_memory_provider_keeps_full_open_slot_list() -> None:
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=date(2026, 9, 15), days=14)
    provider = MemoryBookingProvider(client)
    result = await provider.check_availability(
        branch="shellharbour",
        appointment_type="check-up",
        date_range="2026-09-15/2026-09-28",
        limit=None,
    )
    assert result["ok"] is True
    assert len(result["slots"]) > 12
