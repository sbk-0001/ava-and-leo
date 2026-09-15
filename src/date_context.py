"""Sydney DateContext. resolve_date_phrase is the only date maths Ava may use."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from booking import _WEEKDAYS, parse_date_range

SYDNEY = ZoneInfo("Australia/Sydney")
_CLOCK_PHRASES = frozenset(
    {
        "now",
        "the time",
        "what time",
        "what time is it",
        "what's the time",
        "whats the time",
        "current time",
        "sydney time",
    }
)

_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b",
    re.I,
)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_sydney_date(day: date) -> str:
    return f"{day.strftime('%A')} the {_ordinal(day.day)} of {day.strftime('%B %Y')}"


def aware_sydney(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SYDNEY)
    return moment.astimezone(SYDNEY)


def period_of_day(moment: datetime) -> str:
    """Illawarra desk periods. 17:30 is evening, never morning."""
    hour = aware_sydney(moment).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "arvo"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def format_sydney_clock(moment: datetime) -> str:
    """Short clock, e.g. '5:30 pm'."""
    local = aware_sydney(moment)
    hour12 = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    if local.minute == 0:
        return f"{hour12} {suffix}"
    return f"{hour12}:{local.minute:02d} {suffix}"


def format_sydney_clock_spoken(moment: datetime) -> str:
    """Spoken clock with period, e.g. 'half past five in the evening'."""
    local = aware_sydney(moment)
    hour12 = local.hour % 12 or 12
    names = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
    }
    hour_word = names[hour12]
    minute = local.minute
    period = period_of_day(local)
    if minute == 0:
        clock = f"{hour_word} o'clock"
    elif minute == 15:
        clock = f"quarter past {hour_word}"
    elif minute == 30:
        clock = f"half past {hour_word}"
    elif minute == 45:
        nxt = names[(hour12 % 12) + 1] if hour12 < 12 else "one"
        clock = f"quarter to {nxt}"
    else:
        clock = format_sydney_clock(local)
    return f"{clock} in the {period}"


def current_time_sydney(*, now: datetime | None = None) -> dict[str, Any]:
    """Fact sheet for 'what time is it?'. Never a model guess."""
    ctx = refresh_date_context(now=now)
    return {
        "ok": True,
        "timezone": ctx.timezone,
        "now_iso": ctx.now.isoformat(),
        "clock": ctx.clock_short,
        "spoken": ctx.clock_spoken,
        "period": ctx.period,
        "today": ctx.today_iso,
        "today_spoken": ctx.today_spoken,
        "note": (
            "This is the current Australia/Sydney clock. Answer from this fact. "
            "Do not invent another time of day. Never offer a diary slot that "
            "has already started."
        ),
    }


@dataclass
class DateContext:
    today: date
    timezone: str = "Australia/Sydney"
    now: datetime = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.now is None:
            self.now = datetime.combine(self.today, time.min, tzinfo=SYDNEY)
        else:
            self.now = aware_sydney(self.now)
            self.today = self.now.date()

    @property
    def today_iso(self) -> str:
        return self.today.isoformat()

    @property
    def tomorrow(self) -> date:
        return self.today + timedelta(days=1)

    @property
    def weekday(self) -> str:
        return self.today.strftime("%A")

    @property
    def today_spoken(self) -> str:
        return format_sydney_date(self.today)

    @property
    def tomorrow_spoken(self) -> str:
        return format_sydney_date(self.tomorrow)

    @property
    def period(self) -> str:
        return period_of_day(self.now)

    @property
    def clock_short(self) -> str:
        return format_sydney_clock(self.now)

    @property
    def clock_spoken(self) -> str:
        return format_sydney_clock_spoken(self.now)

    def prompt_line(self) -> str:
        period = self.period
        not_morning = (
            " It is not morning."
            if period != "morning"
            else " It is morning until noon."
        )
        return (
            f"DATE CONTEXT (Australia/Sydney, do not do date maths yourself, "
            f"do not invent the clock): "
            f"today is {self.today_spoken} (ISO {self.today_iso}). "
            f"The current clock is {self.clock_spoken} "
            f"({self.clock_short}, ISO {self.now.isoformat()}). "
            f"It is {period}.{not_morning} "
            f"Tomorrow is {self.tomorrow_spoken} (ISO {self.tomorrow.isoformat()}). "
            f"If the caller asks what time it is, answer from this clock fact. "
            f"Never offer a diary time that has already passed today. "
            f"Call resolve_date_phrase for any other day. "
            f"Call current_time_sydney if you need the clock restated. "
            f"If it comes back ambiguous, ask the caller."
        )


def refresh_date_context(
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> DateContext:
    if now is not None:
        current = aware_sydney(now)
        return DateContext(today=current.date(), now=current)
    if today is not None:
        return DateContext(today=today)
    current = datetime.now(SYDNEY)
    return DateContext(today=current.date(), now=current)


def _days_until_weekday(today: date, weekday: int) -> int:
    delta = (weekday - today.weekday()) % 7
    return delta


def resolve_date_phrase(
    phrase: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a spoken date. Ambiguous next-weekday asks the human."""
    clock = aware_sydney(now) if now is not None else datetime.now(SYDNEY)
    day = today or clock.date()
    raw = (phrase or "").strip()
    payload: dict[str, Any] = {
        "ok": True,
        "phrase": raw,
        "resolved": False,
        "ambiguous": False,
        "start": None,
        "end": None,
        "spoken": None,
        "ask": None,
        "timezone": "Australia/Sydney",
        "today": day.isoformat(),
        "now_iso": clock.isoformat(),
    }
    if not raw:
        payload["ok"] = False
        payload["reason"] = "empty_tool_args"
        payload["note"] = "Pass the caller's date phrase. Do not guess."
        return payload

    lowered = raw.lower().strip(" ?!.")
    if lowered in _CLOCK_PHRASES or lowered.startswith("what time"):
        clock_fact = current_time_sydney(now=clock)
        clock_fact["phrase"] = raw
        return clock_fact

    now = day
    next_weekday = _NEXT_WEEKDAY_RE.search(lowered)
    if next_weekday:
        target = _WEEKDAYS[next_weekday.group(1).lower()]
        until = _days_until_weekday(now, target)
        still_ahead_this_week = now.weekday() < target
        if still_ahead_this_week and 1 <= until <= 6:
            this_day = now + timedelta(days=until)
            next_week_day = this_day + timedelta(days=7)
            weekday_name = this_day.strftime("%A")
            payload["ambiguous"] = True
            payload["ask"] = (
                f"Did you mean this {weekday_name} "
                f"({format_sydney_date(this_day)}) or {weekday_name} next week "
                f"({format_sydney_date(next_week_day)})?"
            )
            payload["note"] = (
                "Ask the caller to pick. Do not choose. Do not search the diary yet."
            )
            payload["candidates"] = [
                {"label": f"this {weekday_name}", "start": this_day.isoformat()},
                {
                    "label": f"{weekday_name} next week",
                    "start": next_week_day.isoformat(),
                },
            ]
            return payload

    start, end = parse_date_range(raw, today=now)
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    payload["resolved"] = True
    payload["start"] = start
    payload["end"] = end
    if start == end:
        payload["spoken"] = format_sydney_date(start_day)
    else:
        payload["spoken"] = (
            f"{format_sydney_date(start_day)} through {format_sydney_date(end_day)}"
        )
    payload["note"] = (
        "Use these ISO dates with check_availability. Do not invent another day."
    )
    return payload
