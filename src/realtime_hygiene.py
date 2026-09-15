"""OpenAI Realtime session hygiene: trim history, recover from TPM limits.

CallState stays the source of truth. Trimming only drops remote conversation
items so long calls stop burning tokens-per-minute.

Docs:
  https://docs.livekit.io/agents/logic/chat-context/#truncating-a-context
  https://docs.livekit.io/agents/logic/agents-handoffs/#summarizing-context
  https://docs.livekit.io/reference/agents/events/#handling-errors
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents.llm import ChatContext

from call_state import CallState

logger = logging.getLogger("ava.realtime")

# ~10-12 spoken turns plus tools. A 14-minute chatty call otherwise ships the
# full transcript (and audio item ids) on every Realtime response.create.
CONTEXT_MAX_ITEMS = 20
ANCHOR_PREFIX = "Earlier on this call"

RATE_LIMIT_COVER_INSTRUCTIONS = (
    "The line hiccupped. One short spoken cover only — 'just a sec' or "
    "'two secs, bear with me'. Australian, in character. Then stop. "
    "Do not recap. Do not mention an error or a limit."
)

RATE_LIMIT_RETRY_INSTRUCTIONS = (
    "Continue from CallState. Answer the last thing they said in one or two "
    "sentences. Do not mention a delay, a glitch, or an error."
)

_RATE_LIMIT_NEEDLES = (
    "rate_limit_exceeded",
    "rate limit exceeded",
    "tokens] rate_limit",
    "inference_rate_limit_exceeded",
)

SleepFn = Callable[[float], Awaitable[None] | None]
TrimFn = Callable[[], Awaitable[None]]


def should_trim_context(item_count: int, max_items: int = CONTEXT_MAX_ITEMS) -> bool:
    return item_count > max_items


def call_state_anchor_text(state: CallState) -> str:
    """Compact recap so dropping old turns does not drop booking facts."""
    return (
        f"{ANCHOR_PREFIX} — facts from CallState, do not contradict:\n"
        f"{state.prompt_block()}\n"
        "Keep replies to one or two sentences. Do not recap the whole call."
    )


def _is_anchor(item: Any) -> bool:
    extra = getattr(item, "extra", None) or {}
    if extra.get("call_state_anchor"):
        return True
    text = getattr(item, "text_content", None) or ""
    return text.startswith(ANCHOR_PREFIX)


def trimmed_chat_context(
    chat_ctx: ChatContext,
    state: CallState,
    *,
    max_items: int = CONTEXT_MAX_ITEMS,
) -> ChatContext:
    """Keep recent items + a CallState system message. Never mutates CallState.

    Uses ChatContext.copy().truncate(); OpenAI Realtime update_chat_ctx then
    sends conversation.item.delete for anything that fell out of the window.
    """
    copied = chat_ctx.copy()
    copied.items[:] = [item for item in copied.items if not _is_anchor(item)]
    keep = max(4, max_items - 1)
    copied.truncate(max_items=keep)
    copied.add_message(
        role="system",
        content=call_state_anchor_text(state),
        extra={"call_state_anchor": True, "is_summary": True},
    )
    return copied


async def maybe_trim_realtime_context(
    agent: Any,
    *,
    max_items: int = CONTEXT_MAX_ITEMS,
) -> bool:
    """Trim agent.chat_ctx when it exceeds the item budget. CallState is untouched."""
    chat_ctx = getattr(agent, "chat_ctx", None)
    state = getattr(agent, "state", None)
    if chat_ctx is None or not isinstance(state, CallState):
        return False
    items = getattr(chat_ctx, "items", None) or []
    if not should_trim_context(len(items), max_items=max_items):
        return False
    before = len(items)
    trimmed = trimmed_chat_context(chat_ctx, state, max_items=max_items)
    await agent.update_chat_ctx(trimmed)
    logger.info(
        "trimmed realtime context from %s to %s items; CallState intact "
        "(branch=%s name=%s mobile=%s slot=%s)",
        before,
        len(trimmed.items),
        state.branch,
        state.caller_name,
        state.caller_mobile,
        state.confirmed_slot or state.proposed_slot,
    )
    return True


def is_rate_limit_error(error: object) -> bool:
    """True for OpenAI Realtime TPM / token rate limits, not billing quota."""
    if error is None:
        return False
    chunks = [str(error)]
    for attr in ("code", "message", "type", "body"):
        value = getattr(error, attr, None)
        if value:
            chunks.append(str(value))
    text = " ".join(chunks).lower()
    if "insufficient_quota" in text or "invalid_api_key" in text:
        return False
    return any(needle in text for needle in _RATE_LIMIT_NEEDLES)


def rate_limit_backoff_s(attempt: int) -> float:
    """1s, 2s, 4s, … capped at 8s."""
    if attempt < 1:
        attempt = 1
    return float(min(8.0, 1.0 * (2 ** (attempt - 1))))


class RateLimitRecovery:
    """Soft recovery: trim, cover ('just a sec'), backoff, then retry."""

    def __init__(self, *, sleep: SleepFn | None = None) -> None:
        self._sleep_fn = sleep
        self._task: asyncio.Task[None] | None = None
        self.attempts = 0

    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()

    def schedule(self, session: Any, *, trim: TrimFn | None = None) -> None:
        if self.in_flight():
            logger.info("rate-limit recovery already in flight; not stacking")
            return
        self._task = asyncio.create_task(
            self.recover(session, trim=trim),
            name="ava-rate-limit-recovery",
        )

    async def recover(self, session: Any, *, trim: TrimFn | None = None) -> None:
        self.attempts += 1
        delay = rate_limit_backoff_s(self.attempts)
        logger.warning(
            "openai realtime rate_limit_exceeded; trimming, covering, "
            "retrying in %.1fs (attempt %s)",
            delay,
            self.attempts,
        )
        if trim is not None:
            try:
                await trim()
            except Exception:
                logger.exception("rate-limit trim failed; CallState still intact")
        try:
            await session.generate_reply(instructions=RATE_LIMIT_COVER_INSTRUCTIONS)
        except Exception:
            logger.exception("rate-limit cover speech failed")
        await self._sleep(delay)
        try:
            await session.generate_reply(instructions=RATE_LIMIT_RETRY_INSTRUCTIONS)
        except Exception:
            logger.exception("rate-limit retry speech failed")

    def reset(self) -> None:
        self.attempts = 0

    async def _sleep(self, seconds: float) -> None:
        if self._sleep_fn is None:
            await asyncio.sleep(seconds)
            return
        result = self._sleep_fn(seconds)
        if inspect.isawaitable(result):
            await result
