"""Caller KnownFacts, mobile re-ask reject, ANI store, DOB gate, 12-month purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from call_state import CallState
from caller_store import (
    CallerRecord,
    CallerStore,
    apply_record_to_state,
    upsert_from_booking,
)
from sip_utils import ani_from_participant, normalize_au_phone

SYDNEY = ZoneInfo("Australia/Sydney")

# Frozen Sydney clock. MemoryBookingProvider defaults now_fn to the live clock,
# so an unpinned provider drops the seeded slots as past once real time moves
# beyond them and these tests fail by calendar date, not by behaviour.
SYDNEY_NOW = datetime(2026, 9, 15, 7, 0, tzinfo=SYDNEY)


def test_mobile_given_once_second_ask_rejected() -> None:
    state = CallState()
    first = state.register_mobile("0412 334 556")
    assert first["ok"] is True
    assert state.caller_mobile == "0412334556"
    second = state.ask_for("mobile")
    assert second["already_known"] is True
    assert "already known: 0412334556" in second["note"]
    again = state.register_mobile("0412334556")
    assert again.get("already_known") is True
    assert "already known" in again["note"]


def test_known_facts_injected_every_turn() -> None:
    state = CallState(
        branch="dapto", caller_name="Jane Cole", caller_mobile="0412000000"
    )
    block = state.known_facts_block()
    assert "KNOWN FACTS" in block
    assert "Jane" in block
    assert "0412000000" in block
    assert "never ask again" in block.lower()
    prompt = state.prompt_block()
    assert "KNOWN FACTS" in prompt


def test_known_ani_greets_first_name_and_skips_number() -> None:
    store = CallerStore(path=None)
    store.touch(
        "+61412345678",
        name="Priya Nair",
        preferred_branch="shellharbour",
        dentist_preference="Dr Mohit Tolani",
    )
    state = CallState(branch="shellharbour")
    record = store.lookup("+61412345678")
    assert record is not None
    apply_record_to_state(state, record)
    assert state.known_caller is True
    assert state.caller_first_name == "Priya"
    assert state.caller_mobile == "0412345678"
    from ava_receptionist import inbound_greeting_instructions

    greet = inbound_greeting_instructions("shellharbour", state=state)
    lowered = greet.lower()
    assert "priya" in lowered
    assert "do not ask for their number" in lowered
    assert "still the best number" in lowered


def test_ani_from_sip_participant() -> None:
    participant = SimpleNamespace(
        attributes={
            "sip.phoneNumber": "0412 345 678",
            "sip.trunkPhoneNumber": "0242169911",
        },
        identity="sip_x",
    )
    assert ani_from_participant(participant) == "+61412345678"
    assert normalize_au_phone("0412345678") == "+61412345678"


@pytest.mark.asyncio
async def test_unverified_lookup_does_not_confirm_patient(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from ava_receptionist import AvaReceptionist
    from booking import MemoryBookingProvider
    from practice import PracticeClient

    class Player:
        def __init__(self) -> None:
            self.played: list[str] = []
            self.last_first_audio_ts = 0.0
            self.session = None
            self.ambient = None
            self.on_audio = None

        async def play(self, text: str, **_kwargs: object) -> None:
            self.played.append(text)

    client = PracticeClient(mode="mock")
    client.seed_patient(
        patient_id="pat_jane",
        name="Jane Cole",
        phone="0412334556",
        date_of_birth="1990-01-15",
    )
    ava = AvaReceptionist(
        state=CallState(branch="shellharbour"),
        booking=MemoryBookingProvider(client, now_fn=lambda: SYDNEY_NOW),
        filler_player=Player(),
    )

    class Ctx:
        session = SimpleNamespace()

    result = await ava.lookup_patient(Ctx(), "0412334556")
    assert result.get("identity_verified") is False
    assert result.get("bookings") == []
    assert result.get("patients") == []
    assert ava.state.pms_record
    assert ava.state.dob_verified is False


def test_failed_dob_offers_callback_without_saying_wrong() -> None:
    state = CallState()
    state.pms_record = {
        "ok": True,
        "is_existing_patient": True,
        "patients": [{"date_of_birth": "1990-01-15", "name": "Jane"}],
        "bookings": [{"booking_id": "bkg_1", "date": "2026-09-16"}],
    }
    failed = state.verify_dob("1980-12-01")
    assert failed["ok"] is False
    assert failed["reason"] == "verification_failed"
    assert failed.get("retry_allowed") is True
    note = failed["note"].lower()
    assert "wrong" not in note
    assert "did not match" in note
    assert "do not confirm" in note
    assert state.dob_verified is False
    assert state.dob_failed is False
    blocked = state.require_dob_for_existing()
    assert blocked["reason"] == "dob_required"
    assert blocked.get("retry_allowed") is True

    second = state.verify_dob("1970-01-01")
    assert second["ok"] is False
    assert second["retry_allowed"] is False
    assert "call back" in second["note"].lower()
    assert "wrong" not in second["note"].lower()
    assert state.dob_failed is True
    assert state.require_dob_for_existing()["reason"] == "verification_failed"


def test_dob_retry_once_then_succeeds_on_second_date() -> None:
    state = CallState()
    state.pms_record = {
        "ok": True,
        "patients": [{"date_of_birth": "1989-08-23", "name": "Sam"}],
    }
    first = state.verify_dob("01-01-1980")
    assert first["retry_allowed"] is True
    ok = state.verify_dob("23rd August 1989")
    assert ok["ok"] is True
    assert ok["verified"] is True
    assert state.dob_verified is True
    assert state.dob_failed is False


def test_empty_record_dob_is_collected_and_verified() -> None:
    state = CallState()
    state.pms_record = {
        "ok": True,
        "patients": [
            {
                "patient_id": "pat_sam",
                "date_of_birth": "",
                "name": "Sam Smith",
            }
        ],
    }
    result = state.verify_dob("23rd August 1989")
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["stored_dob"] == "1989-08-23"
    assert state.pms_record["patients"][0]["date_of_birth"] == "1989-08-23"


def test_new_booking_does_not_need_dob() -> None:
    state = CallState()
    assert state.may_disclose_existing() is False
    # New book path does not call require_dob_for_existing.
    assert state.dob_verified is False


def test_purge_removes_thirteen_month_old_record() -> None:
    store = CallerStore(path=None)
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    store.touch("+61411111111", name="Fresh", now=now)
    old = CallerRecord(
        e164="+61422222222",
        name="Stale",
        last_contacted=(now - timedelta(days=400)).isoformat(),
    )
    store.records[old.e164] = old
    removed = store.purge_older_than(now=now)
    assert "+61422222222" in removed
    assert "+61411111111" not in removed
    assert store.lookup("+61422222222") is None
    assert store.lookup("+61411111111") is not None


def test_successful_booking_writes_caller_store() -> None:
    store = CallerStore(path=None)
    state = CallState(
        branch="shellharbour",
        caller_name="Sam Lee",
        caller_mobile="0413000111",
        preferred_clinician="Dr Pat Pandey",
    )
    upsert_from_booking(
        store,
        state,
        {
            "ok": True,
            "confirmed": True,
            "booking_id": "bkg_786135d6d9",
            "slot_id": "slot_x",
            "clinician": "Dr Pat Pandey",
        },
    )
    record = store.lookup("0413000111")
    assert record is not None
    assert record.name == "Sam Lee"
    assert record.booking_history
    assert record.booking_history[-1]["booking_id"] == "bkg_786135d6d9"
