"""CallState is the single source of truth — hard ask counters, DID branch, urgency."""

from call_state import (
    MAX_ASKS,
    CallState,
    classify_urgency,
    is_valid_au_mobile,
    kill_switch_enabled,
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
