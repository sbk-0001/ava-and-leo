"""Realtime context trim, rate-limit recovery, and supabase prewarm.

Docs: https://docs.livekit.io/agents/logic/chat-context/#truncating-a-context
      https://docs.livekit.io/reference/agents/events/#handling-errors
      https://docs.livekit.io/agents/server/options/#prewarm-function
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit.agents.llm import ChatContext

from call_state import CallState
from phrase_pools import STAGE_2, STAGE_3
from realtime_hygiene import (
    CONTEXT_MAX_ITEMS,
    RATE_LIMIT_COVER_INSTRUCTIONS,
    RATE_LIMIT_RETRY_INSTRUCTIONS,
    RateLimitRecovery,
    call_state_anchor_text,
    is_rate_limit_error,
    maybe_trim_realtime_context,
    parse_retry_after_s,
    rate_limit_backoff_s,
    should_trim_context,
    trimmed_chat_context,
)


def _busy_ctx(n: int) -> ChatContext:
    ctx = ChatContext()
    ctx.add_message(role="system", content="You are Ava.")
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        ctx.add_message(role=role, content=f"turn {i} filler chat about the weather")
    return ctx


def test_should_trim_after_item_budget() -> None:
    assert should_trim_context(CONTEXT_MAX_ITEMS) is False
    assert should_trim_context(CONTEXT_MAX_ITEMS + 1) is True
    assert should_trim_context(4, max_items=8) is False


def test_trim_keeps_recent_turns_and_does_not_wipe_call_state() -> None:
    state = CallState(
        branch="dapto",
        caller_name="Jane",
        caller_mobile="0412334556",
        confirmed_slot="slot-friday-1650",
        intent="booked",
    )
    state.turn_count = 40
    ctx = _busy_ctx(40)
    trimmed = trimmed_chat_context(ctx, state, max_items=12)

    assert state.caller_name == "Jane"
    assert state.caller_mobile == "0412334556"
    assert state.confirmed_slot == "slot-friday-1650"
    assert state.branch == "dapto"
    assert state.turn_count == 40
    assert len(trimmed.items) < len(ctx.items)
    # truncate() may keep the leading instruction plus the tail window.
    assert len(trimmed.items) <= 14
    texts = [item.text_content or "" for item in trimmed.items]
    assert any("turn 39" in text for text in texts)
    assert not any("turn 0 filler" in text for text in texts)


def test_trim_injects_call_state_anchor_once() -> None:
    state = CallState(branch="shellharbour", caller_name="Sam")
    ctx = _busy_ctx(30)
    first = trimmed_chat_context(ctx, state, max_items=10)
    second = trimmed_chat_context(first, state, max_items=10)
    anchors = [
        item
        for item in second.items
        if (item.text_content or "").startswith("Earlier on this call")
        or (getattr(item, "extra", None) or {}).get("call_state_anchor")
    ]
    assert len(anchors) == 1
    assert "Sam" in (anchors[0].text_content or "")
    assert "Shellharbour Dentists" in (anchors[0].text_content or "")


def test_call_state_anchor_includes_booking_facts() -> None:
    state = CallState(
        branch="woonona",
        caller_name="Pat",
        caller_mobile="0412000000",
        proposed_slot="s1",
    )
    text = call_state_anchor_text(state)
    assert "CallState" in text
    assert "Pat" in text
    assert "0412000000" in text
    assert "one or two sentences" in text.lower()


def test_is_rate_limit_error_matches_live_log() -> None:
    err = SimpleNamespace(
        message="response failed: [tokens] rate_limit_exceeded",
        code="rate_limit_exceeded",
        recoverable=True,
    )
    assert is_rate_limit_error(err) is True
    assert is_rate_limit_error(
        "RealtimeError: response failed: [tokens] rate_limit_exceeded"
    )
    assert is_rate_limit_error(SimpleNamespace(code="inference_rate_limit_exceeded"))
    assert is_rate_limit_error(SimpleNamespace(message="hello", code=None)) is False
    assert is_rate_limit_error(SimpleNamespace(code="insufficient_quota")) is False


def test_rate_limit_backoff_grows_and_caps() -> None:
    assert rate_limit_backoff_s(1) == 1.0
    assert rate_limit_backoff_s(2) == 2.0
    assert rate_limit_backoff_s(3) == 4.0
    assert rate_limit_backoff_s(8) == 8.0


def test_parse_retry_after_from_openai_tpm_error() -> None:
    err = SimpleNamespace(
        message=(
            "Rate limit reached for gpt-4o-realtime on tokens per min (TPM): "
            "Limit 40000, Used 40000, Requested 812. Please try again in 6.42s."
        ),
        code="rate_limit_exceeded",
    )
    assert parse_retry_after_s(err) == pytest.approx(6.42)
    assert parse_retry_after_s("please try again in 2s") == 2.0
    assert parse_retry_after_s(SimpleNamespace(message="no hint")) is None
    assert rate_limit_backoff_s(1, retry_after_s=6.42) >= 6.42


async def test_rate_limit_recovery_covers_trims_and_retries() -> None:
    sleeps: list[float] = []
    said: list[str] = []

    class Player:
        async def play(self, text: str, **_kwargs: object) -> None:
            said.append(text)

    session = SimpleNamespace(generate_reply=AsyncMock())
    agent = SimpleNamespace(trim_calls=0)

    async def trim() -> None:
        agent.trim_calls += 1

    recovery = RateLimitRecovery(
        sleep=lambda s: sleeps.append(s) or None, player=Player()
    )
    await recovery.recover(session, trim=trim)

    assert sleeps == [1.0]
    assert agent.trim_calls == 1
    assert said
    assert said[0] in STAGE_2 or said[0] in STAGE_3
    spoken = [
        call.kwargs.get("instructions")
        for call in session.generate_reply.await_args_list
    ]
    assert RATE_LIMIT_RETRY_INSTRUCTIONS in spoken
    assert "just a sec" in RATE_LIMIT_COVER_INSTRUCTIONS.lower()


async def test_rate_limit_recovery_uses_retry_after_and_speaks_slots() -> None:
    sleeps: list[float] = []
    said: list[str] = []

    class Player:
        async def play(self, text: str, **_kwargs: object) -> None:
            said.append(text)

    session = SimpleNamespace(generate_reply=AsyncMock())
    state = CallState(branch="shellharbour")
    state.remember_availability(
        {
            "ok": True,
            "slots": [
                {
                    "date": "2026-09-22",
                    "time": "10:00",
                    "clinician": "Dr Mohit Tolani",
                },
                {
                    "date": "2026-09-23",
                    "time": "14:30",
                    "clinician": "Dr Mohit Tolani",
                },
            ],
        }
    )
    err = SimpleNamespace(
        message="rate_limit_exceeded. Please try again in 4.5s.",
        code="rate_limit_exceeded",
    )
    recovery = RateLimitRecovery(
        sleep=lambda s: sleeps.append(s) or None, player=Player()
    )
    await recovery.recover(session, error=err, state=state)

    assert sleeps
    assert sleeps[0] >= 4.5
    assert said
    session.generate_reply.assert_called()
    spoken = " ".join(
        str(call.kwargs.get("instructions") or "")
        for call in session.generate_reply.await_args_list
    )
    assert "Tuesday" in spoken or "10:00" in spoken
    assert "Mohit" in spoken


async def test_maybe_trim_calls_update_when_over_budget() -> None:
    state = CallState(branch="dapto", caller_name="Lee")
    agent = SimpleNamespace(
        chat_ctx=_busy_ctx(40),
        state=state,
        update_chat_ctx=AsyncMock(),
    )
    did = await maybe_trim_realtime_context(agent, max_items=12)
    assert did is True
    agent.update_chat_ctx.assert_awaited_once()
    sent = agent.update_chat_ctx.await_args.args[0]
    assert len(sent.items) <= 14
    assert state.caller_name == "Lee"


async def test_maybe_trim_is_noop_under_budget() -> None:
    agent = SimpleNamespace(
        chat_ctx=_busy_ctx(4),
        state=CallState(),
        update_chat_ctx=AsyncMock(),
    )
    did = await maybe_trim_realtime_context(agent, max_items=20)
    assert did is False
    agent.update_chat_ctx.assert_not_called()


def test_agent_prewarms_supabase_and_handles_rate_limits() -> None:
    from agent import (
        _get_supabase_create_client,
        _save_call_to_supabase,
        my_agent,
        prewarm,
    )

    save_src = inspect.getsource(_save_call_to_supabase)
    assert "from supabase import" not in save_src
    assert "_get_supabase_create_client" in save_src

    factory_src = inspect.getsource(_get_supabase_create_client)
    assert "from supabase import create_client" in factory_src

    prewarm_src = inspect.getsource(prewarm)
    assert "from supabase import create_client" in prewarm_src
    assert "setup_fnc" in inspect.getsource(inspect.getmodule(prewarm))

    agent_src = inspect.getsource(my_agent)
    assert "is_rate_limit_error" in agent_src
    assert "RateLimitRecovery" in agent_src
    assert "asyncio.to_thread" in agent_src
    assert (
        "maybe_trim_realtime_context" not in agent_src or "AvaReceptionist" in agent_src
    )


def test_ava_trims_on_user_turn() -> None:
    from ava_receptionist import AvaReceptionist

    src = inspect.getsource(AvaReceptionist.on_user_turn_completed)
    assert "maybe_trim_realtime_context" in src
    assert "update_instructions" in src
