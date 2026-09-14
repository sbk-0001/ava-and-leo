"""Practice-software adapter.

Mock and disconnected modes must not invent diary slots. Confirmed is only
returned as true when a book, reschedule, or cancel actually succeeds.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

PracticeMode = Literal["disconnected", "mock"]


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


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
    ) -> None:
        self.slots[slot_id] = Slot(
            slot_id=slot_id,
            branch_id=branch_id,
            date=date,
            time=time,
            clinician=clinician,
        )

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
        return {"ok": True, "slots": slots, "date": date, "branch_id": branch_id}

    async def book_appointment(
        self,
        *,
        branch_id: str,
        slot_id: str,
        patient_id: str,
        reason: str,
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
        if patient_id not in self.patients:
            return {
                "ok": False,
                "reason": "patient_not_found",
                "note": "Find or collect patient details before booking.",
            }

        slot.taken = True
        booking_id = f"bkg_{uuid.uuid4().hex[:10]}"
        booking = Booking(
            booking_id=booking_id,
            slot_id=slot.slot_id,
            branch_id=branch_id,
            patient_id=patient_id,
            date=slot.date,
            time=slot.time,
            clinician=slot.clinician,
            reason=reason,
        )
        self.bookings[booking_id] = booking
        return {
            "ok": True,
            "confirmed": True,
            "booking_id": booking_id,
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
        # Leo can take a message even when the diary is disconnected.
        message = Message(
            message_id=f"msg_{uuid.uuid4().hex[:10]}",
            branch_id=branch_id,
            caller_name=caller_name,
            phone=phone,
            body=body,
        )
        self.messages.append(message)
        return {
            "ok": True,
            "confirmed": True,
            "message_id": message.message_id,
            "branch_id": branch_id,
        }


def practice_from_env() -> PracticeClient:
    raw = os.getenv("PRACTICE_SOFTWARE", "disconnected").strip().lower()
    mode: PracticeMode = "mock" if raw == "mock" else "disconnected"
    return PracticeClient(mode=mode)
