"""DateContext + resolve_date_phrase: no model date maths."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from date_context import (
    DateContext,
    current_time_sydney,
    period_of_day,
    refresh_date_context,
    resolve_date_phrase,
)

SYDNEY = ZoneInfo("Australia/Sydney")
SYDNEY_ARVO = datetime(2026, 9, 15, 17, 30, tzinfo=SYDNEY)


def test_date_context_is_sydney_today() -> None:
    ctx = refresh_date_context(today=date(2026, 9, 15))
    assert ctx.today.isoformat() == "2026-09-15"
    assert ctx.timezone == "Australia/Sydney"
    assert ctx.weekday == "Tuesday"
    assert "Tuesday the 15th of September 2026" in ctx.today_spoken
    assert "2026-09-15" in ctx.prompt_line()


def test_date_context_includes_sydney_clock_at_1730() -> None:
    ctx = refresh_date_context(now=SYDNEY_ARVO)
    line = ctx.prompt_line()
    lowered = line.lower()
    assert ctx.period == "evening"
    assert period_of_day(SYDNEY_ARVO) == "evening"
    assert "5:30 pm" in ctx.clock_short
    assert "17:30" in ctx.now.isoformat()
    assert "half past five" in ctx.clock_spoken
    assert "evening" in ctx.clock_spoken
    assert "do not invent the clock" in lowered
    assert "what time it is" in lowered
    assert "not morning" in lowered
    assert "never offer a diary time that has already passed" in lowered


def test_current_time_sydney_answers_what_time_is_it() -> None:
    hit = current_time_sydney(now=SYDNEY_ARVO)
    assert hit["ok"] is True
    assert hit["period"] == "evening"
    assert "5:30 pm" in hit["clock"]
    assert "2026-09-15T17:30" in hit["now_iso"]
    assert "do not invent" in hit["note"].lower()

    via_tool = resolve_date_phrase("what time is it?", now=SYDNEY_ARVO)
    assert via_tool["ok"] is True
    assert via_tool["period"] == "evening"
    assert via_tool["clock"] == hit["clock"]


def test_resolve_today_tomorrow_next_week() -> None:
    today = date(2026, 9, 15)  # Tuesday
    today_hit = resolve_date_phrase("today", today=today)
    assert today_hit["resolved"] is True
    assert today_hit["ambiguous"] is False
    assert today_hit["start"] == "2026-09-15"
    assert today_hit["end"] == "2026-09-15"

    tomorrow = resolve_date_phrase("tomorrow", today=today)
    assert tomorrow["start"] == "2026-09-16"

    nxt = resolve_date_phrase("next week", today=today)
    assert nxt["resolved"] is True
    assert nxt["start"] == "2026-09-21"
    assert nxt["end"] == "2026-09-27"


def test_next_weekday_after_today_is_resolved() -> None:
    """Wednesday asking for next Tuesday is unambiguously +6 days."""
    today = date(2026, 9, 16)  # Wednesday
    hit = resolve_date_phrase("next Tuesday", today=today)
    assert hit["resolved"] is True
    assert hit["ambiguous"] is False
    assert hit["start"] == "2026-09-22"


def test_next_weekday_still_ahead_this_week_is_ambiguous() -> None:
    """On Monday, 'next Friday' might mean this Friday or Friday week."""
    today = date(2026, 9, 14)  # Monday
    hit = resolve_date_phrase("next Friday", today=today)
    assert hit["ambiguous"] is True
    assert hit["resolved"] is False
    assert hit["ok"] is True
    assert "this friday" in hit["ask"].lower()
    assert "next week" in hit["ask"].lower()


def test_iso_and_this_week_are_resolved() -> None:
    today = date(2026, 9, 15)
    iso = resolve_date_phrase("2026-09-22", today=today)
    assert iso["resolved"] is True
    assert iso["start"] == "2026-09-22"
    week = resolve_date_phrase("this week", today=today)
    assert week["start"] == "2026-09-15"
    assert week["end"] == "2026-09-21"


def test_date_context_dataclass_roundtrip() -> None:
    ctx = DateContext(today=date(2026, 9, 15))
    assert ctx.tomorrow.isoformat() == "2026-09-16"
    assert "Wednesday" in ctx.tomorrow_spoken
