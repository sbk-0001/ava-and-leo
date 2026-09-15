"""Practice-software adapter.

Mock and disconnected modes must not invent diary slots. Confirmed is only
returned as true when a book, reschedule, or cancel actually succeeds.

PRACTICE_SOFTWARE=mock seeds a realistic diary from the official dentist
lists and branch hours, persists in-process (and optionally to JSON) so the
portal and the voice agent can share the same diary.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from persona import BRANCHES, VERIFY

PracticeMode = Literal["disconnected", "mock"]
SYDNEY = ZoneInfo("Australia/Sydney")
DEFAULT_MOCK_PATH = Path(".data/mock_diary.json")

# Demo patients used by the mock diary. Dates of birth are required so
# reschedule / cancel verification can succeed in telephony demos.
DEMO_PATIENTS: tuple[tuple[str, str, str, str], ...] = (
    ("pat_demo_1", "Jordan Blake", "0413000111", "1987-03-12"),
    ("pat_demo_2", "Priya Nair", "0413000222", "1991-11-04"),
    ("pat_demo_3", "Chris O'Neill", "0413000333", "1985-06-21"),
)


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _minutes_to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _iter_slot_times(open_hhmm: str, close_hhmm: str, step: int = 30) -> list[str]:
    start = _hhmm_to_minutes(open_hhmm)
    end = _hhmm_to_minutes(close_hhmm)
    times: list[str] = []
    cursor = start
    while cursor + step <= end:
        times.append(_minutes_to_hhmm(cursor))
        cursor += step
    return times


@dataclass
class Patient:
    patient_id: str
    name: str
    phone: str = ""
    date_of_birth: str = ""


@dataclass
class Slot:
    slot_id: str
    branch_id: str
    date: str
    time: str
    clinician: str
    taken: bool = False


@dataclass
class Booking:
    booking_id: str
    slot_id: str
    branch_id: str
    patient_id: str
    date: str
    time: str
    clinician: str
    reason: str
    cancelled: bool = False


@dataclass
class Message:
    message_id: str
    branch_id: str
    caller_name: str
    phone: str
    body: str


@dataclass
class PracticeClient:
    """Office-system client. Default is disconnected (no invented diary)."""

    mode: PracticeMode = "disconnected"
    persist_path: Path | None = None
    patients: dict[str, Patient] = field(default_factory=dict)
    slots: dict[str, Slot] = field(default_factory=dict)
    bookings: dict[str, Booking] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)

    def _unavailable(self, action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": "practice_software_unavailable",
            "action": action,
            "note": (
                "Diary is not connected. Do not invent times. "
                "Offer to take a message or transfer."
            ),
        }

    def seed_patient(
        self,
        *,
        patient_id: str,
        name: str,
        phone: str = "",
        date_of_birth: str = "",
    ) -> None:
        self.patients[patient_id] = Patient(
            patient_id=patient_id,
            name=name,
            phone=phone,
            date_of_birth=date_of_birth,
        )

    def seed_slot(
        self,
        *,
        slot_id: str,
        branch_id: str,
        date: str,
        time: str,
        clinician: str,
        taken: bool = False,
    ) -> None:
        self.slots[slot_id] = Slot(
            slot_id=slot_id,
            branch_id=branch_id,
            date=date,
            time=time,
            clinician=clinician,
            taken=taken,
        )

    def _ensure_patient(
        self,
        *,
        patient_id: str | None,
        name: str | None,
        phone: str | None,
        date_of_birth: str | None,
    ) -> str | None:
        if patient_id and patient_id in self.patients:
            patient = self.patients[patient_id]
            if date_of_birth and not patient.date_of_birth:
                patient.date_of_birth = date_of_birth
                self.save()
            return patient_id
        if name:
            new_id = patient_id or f"pat_{uuid.uuid4().hex[:10]}"
            self.seed_patient(
                patient_id=new_id,
                name=name,
                phone=phone or "",
                date_of_birth=date_of_birth or "",
            )
            return new_id
        return None

    async def find_patient(
        self,
        *,
        name: str,
        phone: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("find_patient")

        name_n = _norm_text(name)
        phone_n = _norm_phone(phone or "")
        dob_n = (date_of_birth or "").strip()
        matches: list[dict[str, str]] = []
        for patient in self.patients.values():
            if _norm_text(patient.name) != name_n:
                continue
            if phone_n and _norm_phone(patient.phone) != phone_n:
                continue
            if dob_n and patient.date_of_birth != dob_n:
                continue
            matches.append(
                {
                    "patient_id": patient.patient_id,
                    "name": patient.name,
                    "phone": patient.phone,
                    "date_of_birth": patient.date_of_birth,
                }
            )
        return {"ok": True, "patients": matches}

    async def get_availability(
        self,
        *,
        branch_id: str,
        date: str,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("get_availability")

        slots = [
            {
                "slot_id": slot.slot_id,
                "date": slot.date,
                "time": slot.time,
                "clinician": slot.clinician,
                "branch_id": slot.branch_id,
            }
            for slot in self.slots.values()
            if not slot.taken
            and slot.branch_id == branch_id
            and slot.date == date
            and (not clinician or _norm_text(slot.clinician) == _norm_text(clinician))
        ]
        slots.sort(key=lambda item: (item["time"], item["clinician"]))
        return {"ok": True, "slots": slots, "date": date, "branch_id": branch_id}

    async def list_diary(
        self,
        *,
        branch_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("list_diary")

        slots = []
        for slot in self.slots.values():
            if slot.branch_id != branch_id:
                continue
            if date_from and slot.date < date_from:
                continue
            if date_to and slot.date > date_to:
                continue
            if clinician and _norm_text(slot.clinician) != _norm_text(clinician):
                continue
            slots.append(
                {
                    "slot_id": slot.slot_id,
                    "date": slot.date,
                    "time": slot.time,
                    "clinician": slot.clinician,
                    "branch_id": slot.branch_id,
                    "taken": slot.taken,
                }
            )
        slots.sort(key=lambda item: (item["date"], item["time"], item["clinician"]))

        bookings = []
        for booking in self.bookings.values():
            if booking.cancelled or booking.branch_id != branch_id:
                continue
            if date_from and booking.date < date_from:
                continue
            if date_to and booking.date > date_to:
                continue
            patient = self.patients.get(booking.patient_id)
            bookings.append(
                {
                    "booking_id": booking.booking_id,
                    "slot_id": booking.slot_id,
                    "branch_id": booking.branch_id,
                    "patient_id": booking.patient_id,
                    "patient_name": patient.name if patient else "",
                    "patient_phone": patient.phone if patient else "",
                    "date": booking.date,
                    "time": booking.time,
                    "clinician": booking.clinician,
                    "reason": booking.reason,
                }
            )
        bookings.sort(key=lambda item: (item["date"], item["time"]))
        return {
            "ok": True,
            "branch_id": branch_id,
            "slots": slots,
            "bookings": bookings,
        }

    async def book_appointment(
        self,
        *,
        branch_id: str,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        phone: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("book_appointment")

        slot = self.slots.get(slot_id)
        if slot is None or slot.taken or slot.branch_id != branch_id:
            return {
                "ok": False,
                "reason": "slot_unavailable",
                "note": "That time is not in the diary. Do not invent another time.",
            }
        resolved = self._ensure_patient(
            patient_id=patient_id,
            name=name,
            phone=phone,
            date_of_birth=date_of_birth,
        )
        if resolved is None:
            need_fields: list[str] = []
            if not (name or "").strip():
                need_fields.append("name")
            if not (phone or "").strip():
                need_fields.append("mobile")
            return {
                "ok": False,
                "reason": "need_fields" if need_fields else "patient_not_found",
                "need_fields": need_fields or ["name"],
                "note": (
                    "Collect name and mobile before booking. Ask now. "
                    "Do not sit in silence."
                ),
            }

        slot.taken = True
        booking_id = f"bkg_{uuid.uuid4().hex[:10]}"
        booking = Booking(
            booking_id=booking_id,
            slot_id=slot.slot_id,
            branch_id=branch_id,
            patient_id=resolved,
            date=slot.date,
            time=slot.time,
            clinician=slot.clinician,
            reason=reason,
        )
        self.bookings[booking_id] = booking
        self.save()
        return {
            "ok": True,
            "confirmed": True,
            "booking_id": booking_id,
            "patient_id": resolved,
            "date": booking.date,
            "time": booking.time,
            "clinician": booking.clinician,
            "branch_id": branch_id,
        }

    async def reschedule_appointment(
        self,
        *,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("reschedule_appointment")

        booking = self.bookings.get(booking_id)
        new_slot = self.slots.get(new_slot_id)
        if booking is None or booking.cancelled:
            return {"ok": False, "reason": "booking_not_found"}
        if new_slot is None or new_slot.taken:
            return {
                "ok": False,
                "reason": "slot_unavailable",
                "note": "That time is not in the diary. Do not invent another time.",
            }

        old = self.slots.get(booking.slot_id)
        if old is not None:
            old.taken = False
        new_slot.taken = True
        booking.slot_id = new_slot.slot_id
        booking.date = new_slot.date
        booking.time = new_slot.time
        booking.clinician = new_slot.clinician
        booking.branch_id = new_slot.branch_id
        self.save()
        return {
            "ok": True,
            "confirmed": True,
            "booking_id": booking.booking_id,
            "date": booking.date,
            "time": booking.time,
            "clinician": booking.clinician,
            "branch_id": booking.branch_id,
        }

    async def cancel_appointment(self, *, booking_id: str) -> dict[str, Any]:
        if self.mode == "disconnected":
            return self._unavailable("cancel_appointment")

        booking = self.bookings.get(booking_id)
        if booking is None or booking.cancelled:
            return {"ok": False, "reason": "booking_not_found"}

        booking.cancelled = True
        slot = self.slots.get(booking.slot_id)
        if slot is not None:
            slot.taken = False
        self.save()
        return {
            "ok": True,
            "confirmed": True,
            "booking_id": booking.booking_id,
        }

    async def leave_message(
        self,
        *,
        branch_id: str,
        caller_name: str,
        phone: str,
        body: str,
    ) -> dict[str, Any]:
        # Ava can take a message even when the diary is disconnected.
        message = Message(
            message_id=f"msg_{uuid.uuid4().hex[:10]}",
            branch_id=branch_id,
            caller_name=caller_name,
            phone=phone,
            body=body,
        )
        self.messages.append(message)
        self.save()
        return {
            "ok": True,
            "confirmed": True,
            "message_id": message.message_id,
            "branch_id": branch_id,
        }

    def save(self) -> None:
        if self.persist_path is None:
            return
        path = Path(self.persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "patients": {key: asdict(value) for key, value in self.patients.items()},
            "slots": {key: asdict(value) for key, value in self.slots.items()},
            "bookings": {key: asdict(value) for key, value in self.bookings.items()},
            "messages": [asdict(item) for item in self.messages],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self) -> None:
        if self.persist_path is None:
            return
        path = Path(self.persist_path)
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.patients = {
            key: Patient(**value) for key, value in payload.get("patients", {}).items()
        }
        self.slots = {
            key: Slot(**value) for key, value in payload.get("slots", {}).items()
        }
        self.bookings = {
            key: Booking(**value) for key, value in payload.get("bookings", {}).items()
        }
        self.messages = [Message(**item) for item in payload.get("messages", [])]

    def record_date_of_birth(
        self,
        *,
        date_of_birth: str,
        patient_id: str | None = None,
        phone: str | None = None,
    ) -> bool:
        dob = (date_of_birth or "").strip()
        if not dob:
            return False
        patient = self.patients.get(patient_id or "")
        if patient is None and phone:
            digits = _norm_phone(phone)
            for item in self.patients.values():
                stored = _norm_phone(item.phone)
                if stored == digits or (
                    len(digits) >= 9 and stored.endswith(digits[-9:])
                ):
                    patient = item
                    break
        if patient is None:
            return False
        patient.date_of_birth = dob
        self.save()
        return True


def seed_mock_diary(
    client: PracticeClient,
    *,
    today: date | None = None,
    days: int = 14,
    replace: bool = False,
) -> int:
    """Fill open slots across the next `days` using real dentist names and hours."""
    if client.slots and not replace:
        return 0

    start = today or datetime.now(SYDNEY).date()
    created = 0
    for patient_id, name, phone, dob in DEMO_PATIENTS:
        existing = client.patients.get(patient_id)
        if existing is None:
            client.seed_patient(
                patient_id=patient_id,
                name=name,
                phone=phone,
                date_of_birth=dob,
            )
        elif not existing.date_of_birth:
            existing.date_of_birth = dob

    for offset in range(days):
        day = start + timedelta(days=offset)
        weekday = day.weekday()  # Mon=0
        if weekday == 6:
            continue
        for branch in BRANCHES.values():
            hours = branch.clinic_hours
            dentists = [name for name in branch.dentists if name and name != VERIFY]
            if not dentists:
                # Brief did not name dentists for this site — still seed diary
                # slots so Ava can offer times without inventing clinician names.
                dentists = ["available dentist"]
            if weekday == 5:
                if not hours.saturday_open or not hours.saturday_close:
                    continue
                times = _iter_slot_times(hours.saturday_open, hours.saturday_close)
                if hours.saturday_by_appointment:
                    times = times[:2]
            else:
                times = _iter_slot_times(hours.weekday_open, hours.weekday_close)
            for time_index, time in enumerate(times):
                clinician = dentists[time_index % len(dentists)]
                slot_id = (
                    f"slot_{branch.id}_{day.isoformat()}_{time.replace(':', '')}"
                    f"_{_slug(clinician)}"
                )
                taken = created % 11 == 0 and not hours.saturday_by_appointment
                client.seed_slot(
                    slot_id=slot_id,
                    branch_id=branch.id,
                    date=day.isoformat(),
                    time=time,
                    clinician=clinician,
                    taken=taken,
                )
                if taken:
                    patient_id = f"pat_demo_{(created % 3) + 1}"
                    booking_id = f"bkg_seed_{created:04d}"
                    client.bookings[booking_id] = Booking(
                        booking_id=booking_id,
                        slot_id=slot_id,
                        branch_id=branch.id,
                        patient_id=patient_id,
                        date=day.isoformat(),
                        time=time,
                        clinician=clinician,
                        reason="existing booking",
                    )
                created += 1
    return created


def backfill_demo_patient_dobs(client: PracticeClient) -> None:
    """Fill empty DOBs on persisted demo patients so verification can succeed."""
    by_phone = {_norm_phone(phone): dob for _pid, _name, phone, dob in DEMO_PATIENTS}
    for patient_id, _name, _phone, dob in DEMO_PATIENTS:
        patient = client.patients.get(patient_id)
        if patient is not None and not patient.date_of_birth:
            patient.date_of_birth = dob
    for patient in client.patients.values():
        if patient.date_of_birth:
            continue
        matched = by_phone.get(_norm_phone(patient.phone))
        if matched:
            patient.date_of_birth = matched


_SHARED: PracticeClient | None = None


def reset_shared_practice() -> None:
    global _SHARED
    _SHARED = None


def _is_production_start() -> bool:
    return "start" in sys.argv


def _resolve_mode(
    *,
    is_telephony: bool,
    env: Mapping[str, str],
) -> PracticeMode:
    raw = str(env.get("PRACTICE_SOFTWARE", "")).strip().lower()
    if raw in {"mock", "disconnected"}:
        return raw  # type: ignore[return-value]
    if is_telephony or _is_production_start():
        return "disconnected"
    return "mock"


def practice_from_env(
    *,
    is_telephony: bool = False,
    persist: bool = True,
    env: Mapping[str, str] | None = None,
) -> PracticeClient:
    environ = env if env is not None else os.environ
    mode = _resolve_mode(is_telephony=is_telephony, env=environ)
    path: Path | None = None
    if persist and mode == "mock":
        raw_path = str(environ.get("MOCK_DIARY_PATH", "")).strip()
        path = Path(raw_path) if raw_path else DEFAULT_MOCK_PATH
    client = PracticeClient(mode=mode, persist_path=path)
    if mode == "mock":
        client.load()
        before = {
            pid: patient.date_of_birth for pid, patient in client.patients.items()
        }
        backfill_demo_patient_dobs(client)
        if not client.slots:
            seed_mock_diary(client)
            client.save()
        elif any(
            client.patients[pid].date_of_birth != dob for pid, dob in before.items()
        ):
            client.save()
    return client


def get_shared_practice(
    *,
    is_telephony: bool = False,
    persist: bool = True,
    env: Mapping[str, str] | None = None,
) -> PracticeClient:
    """In-process singleton so the portal and Ava share one diary."""
    global _SHARED
    if _SHARED is None:
        _SHARED = practice_from_env(is_telephony=is_telephony, persist=persist, env=env)
    return _SHARED
