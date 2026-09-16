"""Anti-repetition pools live on CallState, not in the prompt."""

from call_state import CallState
from phrase_pools import ACKS, BARGE_IN_RESUME, CLOSINGS, OPENINGS, pick_from_pool


def test_acks_never_repeat_no_worries_in_one_call() -> None:
    state = CallState(branch="shellharbour")
    seen = [state.pick_ack() for _ in ACKS]
    assert len(set(seen)) == len(ACKS)
    assert "no worries" in ACKS
    assert "no dramas" in ACKS
    assert "all good" in ACKS
    assert "too easy" in ACKS
    # After the pool is exhausted, wrap is allowed.
    again = state.pick_ack()
    assert again in ACKS
    assert state.stock_phrases_used
    assert "no worries" in state.prompt_block() or "used_acks" in state.prompt_block()


def test_openings_and_closings_rotate() -> None:
    state = CallState(branch="dapto")
    openings = [state.pick_opening() for _ in OPENINGS]
    assert len(set(openings)) == len(OPENINGS)
    # The opening names the practice, not the clinic whose number rang.
    assert all("Illawarra Dentists" in line for line in openings)
    assert not any("Dapto Dentists" in line for line in openings)
    assert any("how ya going" in line for line in openings)
    closings = [state.pick_closing() for _ in CLOSINGS]
    assert len(set(closings)) == len(CLOSINGS)


def test_barge_in_resume_never_restarts_sentence() -> None:
    state = CallState()
    first = state.mark_interrupted()
    assert first in BARGE_IN_RESUME
    assert state.barge_in_pending is True
    block = state.prompt_block().lower()
    assert "never restart the cut-off sentence" in block
    assert first in state.prompt_block()
    consumed = state.consume_barge_in_resume()
    assert consumed == first
    assert state.consume_barge_in_resume() is None


def test_pick_from_pool_wraps_only_when_exhausted() -> None:
    used: list[str] = []
    pool = ("a", "b")
    first = pick_from_pool(used, pool, rng=__import__("random").Random(0))
    second = pick_from_pool(used, pool, rng=__import__("random").Random(1))
    assert {first, second} == {"a", "b"}
    third = pick_from_pool(used, pool, rng=__import__("random").Random(2))
    assert third in pool
