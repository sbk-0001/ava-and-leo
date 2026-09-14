"""Mock/disconnected practice software must not invent diary slots."""

import pytest

from practice import PracticeClient


@pytest.mark.asyncio
async def test_disconnected_does_not_invent_slots() -> None:
    client = PracticeClient(mode="disconnected")
    result = await client.get_availability(branch_id="shellharbour", date="2026-09-21")
    assert result["ok"] is False
    assert result["reason"] == "practice_software_unavailable"
    assert result.get("slots") in (None, [])
    assert result.get("confirmed") is not True


@pytest.mark.asyncio
async def test_disconnected_mutations_are_not_confirmed() -> None:
    client = PracticeClient(mode="disconnected")
    booked = await client.book_appointment(
        branch_id="shellharbour",
        slot_id="slot-1",
        patient_id="p1",
        reason="check up",
    )
    assert booked["ok"] is False
    assert booked.get("confirmed") is not True

    cancelled = await client.cancel_appointment(booking_id="b1")
    assert cancelled["ok"] is False
    assert cancelled.get("confirmed") is not True


@pytest.mark.asyncio
async def test_mock_empty_diary_returns_no_slots() -> None:
    client = PracticeClient(mode="mock")
    result = await client.get_availability(branch_id="shellharbour", date="2026-09-21")
    assert result["ok"] is True
    assert result["slots"] == []
    assert result.get("confirmed") is not True


@pytest.mark.asyncio
async def test_mock_only_returns_seeded_slots() -> None:
    client = PracticeClient(mode="mock")
    client.seed_slot(
        slot_id="slot-am",
        branch_id="shellharbour",
        date="2026-09-21",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    found = await client.get_availability(branch_id="shellharbour", date="2026-09-21")
    assert found["slots"] == [
        {
            "slot_id": "slot-am",
            "date": "2026-09-21",
            "time": "09:30",
            "clinician": "Dr Mohit Tolani",
            "branch_id": "shellharbour",
        }
    ]
    other_day = await client.get_availability(
        branch_id="shellharbour", date="2026-09-22"
    )
    assert other_day["slots"] == []
    other_branch = await client.get_availability(branch_id="dapto", date="2026-09-21")
    assert other_branch["slots"] == []


@pytest.mark.asyncio
async def test_mock_book_requires_real_slot_then_confirms() -> None:
    client = PracticeClient(mode="mock")
    missing = await client.book_appointment(
        branch_id="shellharbour",
        slot_id="does-not-exist",
        patient_id="p1",
        reason="check up",
    )
    assert missing["ok"] is False
    assert missing.get("confirmed") is not True

    client.seed_patient(patient_id="p1", name="Alex Taylor", phone="0411111111")
    client.seed_slot(
        slot_id="slot-am",
        branch_id="shellharbour",
        date="2026-09-21",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    booked = await client.book_appointment(
        branch_id="shellharbour",
        slot_id="slot-am",
        patient_id="p1",
        reason="check up",
    )
    assert booked["ok"] is True
    assert booked["confirmed"] is True
    assert booked["booking_id"]

    # Slot is consumed; it must not be offered again.
    leftover = await client.get_availability(
        branch_id="shellharbour", date="2026-09-21"
    )
    assert leftover["slots"] == []


@pytest.mark.asyncio
async def test_find_patient_does_not_invent_records() -> None:
    client = PracticeClient(mode="mock")
    missing = await client.find_patient(name="Nobody Smith", phone="0400000000")
    assert missing["ok"] is True
    assert missing["patients"] == []

    client.seed_patient(patient_id="p1", name="Alex Taylor", phone="0411111111")
    found = await client.find_patient(name="alex taylor", phone="0411 111 111")
    assert len(found["patients"]) == 1
    assert found["patients"][0]["patient_id"] == "p1"
