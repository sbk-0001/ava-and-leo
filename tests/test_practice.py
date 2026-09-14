"""Mock/disconnected practice software must not invent diary slots."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from diary_store import DictDiaryStore, RedisDiaryStore, resolve_diary_store_kind
from persona import BRANCHES, VERIFY
from practice import (
    PracticeClient,
    practice_from_env,
    reset_shared_practice,
    seed_mock_diary,
)


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
async def test_mock_book_reschedule_cancel_confirmed() -> None:
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
    client.seed_slot(
        slot_id="slot-pm",
        branch_id="shellharbour",
        date="2026-09-21",
        time="14:00",
        clinician="Dr Amy Min",
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

    leftover = await client.get_availability(
        branch_id="shellharbour", date="2026-09-21"
    )
    assert [slot["slot_id"] for slot in leftover["slots"]] == ["slot-pm"]

    moved = await client.reschedule_appointment(
        booking_id=booked["booking_id"],
        new_slot_id="slot-pm",
    )
    assert moved["ok"] is True
    assert moved["confirmed"] is True
    assert moved["time"] == "14:00"
    assert moved["clinician"] == "Dr Amy Min"

    after_move = await client.get_availability(
        branch_id="shellharbour", date="2026-09-21"
    )
    assert [slot["slot_id"] for slot in after_move["slots"]] == ["slot-am"]

    cancelled = await client.cancel_appointment(booking_id=booked["booking_id"])
    assert cancelled["ok"] is True
    assert cancelled["confirmed"] is True

    reopened = await client.get_availability(
        branch_id="shellharbour", date="2026-09-21"
    )
    assert {slot["slot_id"] for slot in reopened["slots"]} == {"slot-am", "slot-pm"}


@pytest.mark.asyncio
async def test_mock_create_on_book_for_new_patient() -> None:
    client = PracticeClient(mode="mock")
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
        reason="new patient check up",
        name="Sam Nguyen",
        phone="0412 000 111",
    )
    assert booked["ok"] is True
    assert booked["confirmed"] is True
    found = await client.find_patient(name="Sam Nguyen", phone="0412000111")
    assert len(found["patients"]) == 1


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


def test_seeded_diary_uses_real_dentists_and_branch_hours() -> None:
    client = PracticeClient(mode="mock")
    today = date(2026, 9, 14)  # Monday
    created = seed_mock_diary(client, today=today, days=14)
    assert created > 0

    monday = today.isoformat()
    sh_slots = [
        slot
        for slot in client.slots.values()
        if slot.branch_id == "shellharbour" and slot.date == monday
    ]
    assert sh_slots
    assert all(slot.time >= "08:00" and slot.time < "17:00" for slot in sh_slots)
    sh_names = {
        slot.clinician
        for slot in client.slots.values()
        if slot.branch_id == "shellharbour"
    }
    for dentist in BRANCHES["shellharbour"].dentists:
        if dentist != VERIFY:
            assert dentist in sh_names

    saturday = (today + timedelta(days=5)).isoformat()
    dapto_sat = [
        slot
        for slot in client.slots.values()
        if slot.branch_id == "dapto" and slot.date == saturday
    ]
    assert dapto_sat
    assert all(slot.time >= "08:00" and slot.time < "16:00" for slot in dapto_sat)

    woonona_sat = [
        slot
        for slot in client.slots.values()
        if slot.branch_id == "woonona" and slot.date == saturday
    ]
    assert woonona_sat
    assert all(slot.time >= "08:00" and slot.time < "17:00" for slot in woonona_sat)


def test_mock_diary_persists_to_json(tmp_path: Path) -> None:
    path = tmp_path / "diary.json"
    client = PracticeClient(mode="mock", persist_path=path)
    seed_mock_diary(client, today=date(2026, 9, 14), days=7)
    client.save()
    assert path.exists()

    restored = PracticeClient(mode="mock", persist_path=path)
    restored.load()
    assert restored.slots
    assert set(restored.slots) == set(client.slots)


def test_practice_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_shared_practice()
    monkeypatch.delenv("PRACTICE_SOFTWARE", raising=False)
    local = practice_from_env(is_telephony=False, persist=False)
    assert local.mode == "mock"
    reset_shared_practice()
    telephony = practice_from_env(is_telephony=True, persist=False)
    assert telephony.mode == "disconnected"
    reset_shared_practice()
    monkeypatch.setenv("PRACTICE_SOFTWARE", "mock")
    forced = practice_from_env(is_telephony=True, persist=False)
    assert forced.mode == "mock"
    reset_shared_practice()


def test_two_clients_share_dict_diary() -> None:
    bucket: dict = {}
    store = DictDiaryStore(bucket)
    first = PracticeClient(mode="mock", store=store, store_kind="memory")
    seed_mock_diary(first, today=date(2026, 9, 14), days=3)
    first.save()
    second = PracticeClient(mode="mock", store=store, store_kind="memory")
    second.load()
    assert set(second.slots) == set(first.slots)


@pytest.mark.asyncio
async def test_redis_store_shares_bookings_across_clients() -> None:
    kv: dict[str, str] = {}

    def rpc(command: list[str]):
        op = command[0]
        if op == "SET":
            kv[command[1]] = command[2]
            return "OK"
        if op == "GET":
            return kv.get(command[1])
        raise AssertionError(command)

    store = RedisDiaryStore(url="https://example.upstash.io", token="t", rpc=rpc)
    writer = PracticeClient(mode="mock", store=store, store_kind="redis")
    writer.seed_patient(patient_id="p1", name="Alex Taylor", phone="0411111111")
    writer.seed_slot(
        slot_id="slot-am",
        branch_id="shellharbour",
        date="2026-09-21",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    booked = await writer.book_appointment(
        branch_id="shellharbour",
        slot_id="slot-am",
        patient_id="p1",
        reason="check up",
    )
    assert booked["confirmed"] is True

    reader = PracticeClient(mode="mock", store=store, store_kind="redis")
    diary = await reader.list_diary(branch_id="shellharbour")
    taken = next(slot for slot in diary["slots"] if slot["slot_id"] == "slot-am")
    assert taken["taken"] is True


def test_diary_store_kind_from_env() -> None:
    assert (
        resolve_diary_store_kind(
            {
                "UPSTASH_REDIS_REST_URL": "https://example.upstash.io",
                "UPSTASH_REDIS_REST_TOKEN": "token",
            }
        )
        == "redis"
    )
    assert resolve_diary_store_kind({"VERCEL": "1"}) == "memory"
    assert resolve_diary_store_kind({}) == "file"
    assert (
        resolve_diary_store_kind(
            {
                "MOCK_DIARY_TABLE": "mock_diary",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "svc",
            }
        )
        == "supabase"
    )
    assert (
        resolve_diary_store_kind({"DIARY_STORE": "memory", "VERCEL": "1"}) == "memory"
    )
