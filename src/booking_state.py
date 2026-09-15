"""Booking flow state machine. Slot ids must come from this session's offer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from booking import invalid_slot_id_result


class BookingPhase(str, Enum):
    IDLE = "IDLE"
    SLOTS_OFFERED = "SLOTS_OFFERED"
    SLOT_SELECTED = "SLOT_SELECTED"
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


IN_PROGRESS = frozenset({BookingPhase.SLOT_SELECTED, BookingPhase.HELD})

END_CALL_BLOCKED = (
    "Booking is mid-flight. Do not hang up. Finish the hold or tell them "
    "it is not locked yet and offer another slot."
)


@dataclass
class BookingState:
    """IDLE → SLOTS_OFFERED → SLOT_SELECTED → HELD → CONFIRMED|FAILED.

    Hold is thin for Thursday: selecting a session slot moves to HELD on book.
    """

    phase: BookingPhase = BookingPhase.IDLE
    session_slot_ids: set[str] = field(default_factory=set)
    selected_slot_id: str | None = None
    held_slot_id: str | None = None
    failure_reason: str | None = None
    callback_task: dict[str, Any] | None = None

    @property
    def confirm_language_allowed(self) -> bool:
        return self.phase == BookingPhase.CONFIRMED

    def can_end_call(self) -> bool:
        return self.phase not in IN_PROGRESS

    def offer_slots(self, slots: list[dict[str, Any]] | None) -> None:
        ids = {
            str(slot.get("slot_id"))
            for slot in slots or []
            if isinstance(slot, dict) and slot.get("slot_id")
        }
        self.session_slot_ids = ids
        self.selected_slot_id = None
        self.held_slot_id = None
        self.failure_reason = None
        self.callback_task = None
        self.phase = BookingPhase.SLOTS_OFFERED if ids else BookingPhase.IDLE

    def select_slot(self, slot_id: str) -> dict[str, Any]:
        wanted = (slot_id or "").strip()
        if wanted not in self.session_slot_ids:
            return invalid_slot_id_result(slot_id)
        self.selected_slot_id = wanted
        self.phase = BookingPhase.SLOT_SELECTED
        self.failure_reason = None
        return {"ok": True, "slot_id": wanted, "phase": self.phase.value}

    def hold(self, slot_id: str | None = None) -> dict[str, Any]:
        wanted = (slot_id or self.selected_slot_id or "").strip()
        selected = self.select_slot(wanted)
        if not selected.get("ok"):
            return selected
        self.held_slot_id = wanted
        self.phase = BookingPhase.HELD
        return {"ok": True, "slot_id": wanted, "phase": self.phase.value}

    def confirm(self, slot_id: str | None = None) -> dict[str, Any]:
        wanted = (slot_id or self.held_slot_id or self.selected_slot_id or "").strip()
        if wanted:
            self.held_slot_id = wanted
            self.selected_slot_id = wanted
            self.session_slot_ids.add(wanted)
        self.phase = BookingPhase.CONFIRMED
        self.failure_reason = None
        return {"ok": True, "confirmed": True, "phase": self.phase.value}

    def fail(
        self,
        reason: str,
        *,
        callback_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.phase = BookingPhase.FAILED
        self.failure_reason = reason
        if callback_task is not None:
            self.callback_task = callback_task
        return {
            "ok": False,
            "confirmed": False,
            "reason": reason,
            "phase": self.phase.value,
            "callback_task": self.callback_task,
        }

    def resolve_failed(self, callback_task: dict[str, Any]) -> dict[str, Any]:
        self.callback_task = dict(callback_task)
        if self.phase != BookingPhase.FAILED:
            self.phase = BookingPhase.FAILED
        return {
            "ok": True,
            "phase": self.phase.value,
            "callback_task": self.callback_task,
        }

    def end_call_guard(self) -> dict[str, Any]:
        if self.can_end_call():
            return {"ok": True, "phase": self.phase.value}
        return {
            "ok": False,
            "reason": "booking_in_progress",
            "phase": self.phase.value,
            "note": END_CALL_BLOCKED,
        }
