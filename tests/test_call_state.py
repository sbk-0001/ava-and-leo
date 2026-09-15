"""CallState is the single source of truth — hard ask counters, DID branch, urgency."""

from datetime import date

from call_state import (
    MAX_ASKS,
    NOT_LOCKED_SAY,
    CallState,
    apply_confirmation_gate,
    classify_urgency,
    format_sydney_date,
    is_valid_au_mobile,
    kill_switch_enabled,
    match_clinician,
    offer_branch_for_suburb,
)


def test_did_branch_is_on_state_before_speech() -> None:
    state = CallState(branch="dapto")
    assert state.branch == "dapto"
    assert state.branch_name == "Dapto Dentists"
    block = state.prompt_block().lower()
    assert "dapto dentists" in block
    assert "they rang this number" in block
    assert "do not ask which branch" in block
    assert "which branch do you want" not in block


def test_mobile_ask_counter_stops_at_three() -> None:
    state = CallState(branch="shellharbour")
    first = state.register_mobile("not a number")
    assert first["ok"] is False
    assert first["attempts"] == 1
    assert first["stop_asking"] is False

    second = state.register_mobile("123")
    assert second["attempts"] == 2
    assert second["stop_asking"] is False

    third = state.register_mobile("")
    assert third["attempts"] == MAX_ASKS
    assert third["stop_asking"] is True
    assert third["offer_callback"] is True

    fourth = state.register_mobile("still no")
    assert fourth["stop_asking"] is True
    assert fourth["offer_callback"] is True
    assert state.ask_counts["mobile"] == MAX_ASKS


def test_valid_mobile_is_stored() -> None:
    state = CallState()
    result = state.register_mobile("0412 334 556")
    assert result["ok"] is True
    assert state.caller_mobile == "0412334556"
    assert is_valid_au_mobile("+61 412 334 556")


def test_figtree_offers_dapto_never_a_menu() -> None:
    assert offer_branch_for_suburb("Figtree", "shellharbour") == "dapto"
    state = CallState(branch="shellharbour")
    state.observe_user_text("I live in Figtree actually")
    assert state.offered_branch == "dapto"
    block = state.prompt_block().lower()
    assert "dapto" in block
    assert "never a menu" in block


def test_swallowing_is_emergency_and_blocks_booking() -> None:
    assert classify_urgency("my face is swollen and I have trouble swallowing") == (
        "emergency_000"
    )
    state = CallState(branch="shellharbour")
    state.observe_user_text("swollen face and trouble swallowing")
    assert state.urgency_level == "emergency_000"
    assert state.escalation_flag is True
    assert state.may_book() is False


def test_severe_pain_late_friday_is_same_day() -> None:
    state = CallState(branch="shellharbour")
    state.observe_user_text("pain keeping me awake, it's 4:50 on Friday")
    assert state.urgency_level == "same_day"
    assert state.may_book() is True


def test_kill_switch_env(monkeypatch) -> None:
    monkeypatch.delenv("AVA_KILL_SWITCH", raising=False)
    assert kill_switch_enabled(env={}) is False
    assert kill_switch_enabled(env={"AVA_KILL_SWITCH": "1"}) is True
    assert kill_switch_enabled(env={"AVA_KILL_SWITCH": "true"}) is True
    state = CallState(branch="shellharbour", kill_switch=True)
    assert state.kill_switch is True


def test_bot_ask_count_increments() -> None:
    state = CallState()
    state.observe_user_text("Are you a real person?")
    state.observe_user_text("No but are you a bot?")
    assert state.bot_ask_count == 2


def test_sydney_today_is_injected_into_prompt_block() -> None:
    state = CallState(branch="shellharbour", today=date(2026, 9, 15))
    assert state.today.weekday() == 1  # Tuesday
    assert format_sydney_date(state.today) == "Tuesday the 15th of September 2026"
    block = state.prompt_block()
    assert "Tuesday the 15th of September 2026" in block
    assert "2026-09-15" in block
    assert "Wednesday the 16th of September 2026" in block
    assert "2026-09-16" in block
    assert "Monday the 14th" not in block
    assert "do not guess the weekday" in block.lower()


def test_sydney_clock_is_injected_at_1730() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 9, 15, 17, 30, tzinfo=ZoneInfo("Australia/Sydney"))
    state = CallState(branch="shellharbour", now=now)
    block = state.prompt_block().lower()
    assert "5:30 pm" in block or "17:30" in block
    assert "evening" in block
    assert "not morning" in block
    assert "do not invent the clock" in block
    assert "what time it is" in block
    assert "current_time_sydney" in block


def test_no_confirm_without_successful_book() -> None:
    state = CallState(branch="shellharbour", today=date(2026, 9, 15))
    assert state.may_confirm_booking() is False
    assert state.may_offer_times() is False
    block = state.prompt_block().lower()
    assert "booking_locked: false" in block
    assert "you're all set" in block
    assert "not locked yet" in block
    assert "call check_availability first" in block

    failed = state.record_book_result(
        {
            "ok": False,
            "confirmed": False,
            "reason": "slot_gone",
            "slot_id": "slot_shellharbour_2026-09-15_1430_dr-mohit-tolani",
        }
    )
    assert failed["confirmed"] is False
    assert "not locked" in failed["say"].lower()
    assert state.may_confirm_booking() is False
    assert state.confirmed_slot is None
    assert "slot_gone" in state.prompt_block()

    locked = state.record_book_result(
        {
            "ok": True,
            "confirmed": True,
            "slot_id": "slot_shellharbour_2026-09-16_1010_dr-mohit-tolani",
        }
    )
    assert "all set" in locked["say"].lower() or "locked" in locked["say"].lower()
    assert state.may_confirm_booking() is True
    assert state.confirmed_slot == "slot_shellharbour_2026-09-16_1010_dr-mohit-tolani"
    assert "booking_locked: True" in state.prompt_block()


def test_apply_confirmation_gate_rejects_ok_without_confirmed() -> None:
    gated = apply_confirmation_gate(
        {"ok": True, "confirmed": False, "reason": "pending"}
    )
    assert gated["confirmed"] is False
    assert gated["say"] == NOT_LOCKED_SAY


def test_caller_naming_dentist_sets_preferred_clinician() -> None:
    state = CallState(branch="shellharbour")
    state.observe_user_text("Can I see Dr Mohit this week, broken tooth")
    assert state.preferred_clinician == "Dr Mohit Tolani"
    assert "mohit" in state.prompt_block().lower()
    assert match_clinician("I want Dr Tolani") == "Dr Mohit Tolani"


def test_may_offer_times_only_after_availability_slots() -> None:
    state = CallState()
    assert state.may_offer_times() is False
    state.remember_availability({"ok": True, "slots": []})
    assert state.may_offer_times() is False
    state.remember_availability(
        {
            "ok": True,
            "slots": [
                {
                    "date": "2026-09-16",
                    "time": "10:10",
                    "clinician": "Dr Mohit Tolani",
                    "slot_id": "slot_shellharbour_2026-09-16_1010_dr-mohit-tolani",
                }
            ],
        }
    )
    assert state.may_offer_times() is True
    assert "10:10" in state.prompt_block()
    assert "Dr Mohit Tolani" in state.prompt_block()
