"""BookingState: IDLE → SLOTS_OFFERED → SLOT_SELECTED → HELD → CONFIRMED|FAILED."""

from booking import invalid_slot_id_result
from booking_state import BookingPhase, BookingState


def test_offer_select_hold_confirm() -> None:
    flow = BookingState()
    assert flow.phase == BookingPhase.IDLE
    flow.offer_slots(
        [
            {"slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"},
            {"slot_id": "slot_shellharbour_2026-09-16_1545_dr-mohit-tolani"},
        ]
    )
    assert flow.phase == BookingPhase.SLOTS_OFFERED
    selected = flow.select_slot("slot_shellharbour_2026-09-16_1110_dr-mohit-tolani")
    assert selected["ok"] is True
    assert flow.phase == BookingPhase.SLOT_SELECTED
    assert flow.can_end_call() is False
    flow.hold()
    assert flow.phase == BookingPhase.HELD
    assert flow.can_end_call() is False
    flow.confirm()
    assert flow.phase == BookingPhase.CONFIRMED
    assert flow.can_end_call() is True
    assert flow.confirm_language_allowed is True


def test_non_session_slot_id_is_invalid() -> None:
    flow = BookingState()
    flow.offer_slots([{"slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"}])
    rejected = flow.select_slot("slot_shellharbour_2026-09-16_0800_dr-pat-collins")
    assert rejected["reason"] == "invalid_slot_id"
    assert rejected["confirmed"] is False
    assert flow.phase == BookingPhase.SLOTS_OFFERED
    assert flow.selected_slot_id is None


def test_invented_slot_id_invalid_even_when_idle() -> None:
    flow = BookingState()
    rejected = flow.select_slot("slot-8-30-tuesday-dr-mohit-tolani-follow-up")
    assert rejected["reason"] == "invalid_slot_id"
    assert rejected == invalid_slot_id_result(
        "slot-8-30-tuesday-dr-mohit-tolani-follow-up"
    )


def test_failed_sets_callback_task() -> None:
    flow = BookingState()
    flow.offer_slots([{"slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"}])
    flow.select_slot("slot_shellharbour_2026-09-16_1110_dr-mohit-tolani")
    flow.hold()
    flow.fail("slot_gone", callback_task={"action": "take_message"})
    assert flow.phase == BookingPhase.FAILED
    assert flow.failure_reason == "slot_gone"
    assert flow.callback_task == {"action": "take_message"}
    assert flow.can_end_call() is True
    assert flow.confirm_language_allowed is False
    resolved = flow.resolve_failed({"action": "take_message", "ok": True})
    assert resolved["ok"] is True
    assert flow.callback_task["ok"] is True


def test_end_call_blocked_while_held() -> None:
    flow = BookingState()
    flow.offer_slots([{"slot_id": "slot_shellharbour_2026-09-16_1110_dr-mohit-tolani"}])
    flow.select_slot("slot_shellharbour_2026-09-16_1110_dr-mohit-tolani")
    blocked = flow.end_call_guard()
    assert blocked["ok"] is False
    assert blocked["reason"] == "booking_in_progress"
    flow.hold()
    still = flow.end_call_guard()
    assert still["ok"] is False
    flow.confirm()
    allowed = flow.end_call_guard()
    assert allowed["ok"] is True
