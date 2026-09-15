"""Sydney DateContext. resolve_date_phrase is the only date maths Ava may use."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from booking import _WEEKDAYS, parse_date_range

SYDNEY = ZoneInfo("Australia/Sydney")

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


@dataclass
class DateContext:
    today: date
    timezone: str = "Australia/Sydney"

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

    def prompt_line(self) -> str:
        return (
            f"DATE CONTEXT (Australia/Sydney, do not do date maths yourself): "
            f"today is {self.today_spoken} (ISO {self.today_iso}). "
            f"Tomorrow is {self.tomorrow_spoken} (ISO {self.tomorrow.isoformat()}). "
            f"Call resolve_date_phrase for any other day. "
            f"If it comes back ambiguous, ask the caller."
        )


def refresh_date_context(
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> DateContext:
    if today is not None:
        return DateContext(today=today)
    current = now or datetime.now(SYDNEY)
    return DateContext(today=current.date())


def _days_until_weekday(today: date, weekday: int) -> int:
    delta = (weekday - today.weekday()) % 7
    return delta


def resolve_date_phrase(
    phrase: str,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Resolve a spoken date. Ambiguous next-weekday asks the human."""
    now = today or datetime.now(SYDNEY).date()
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
        "today": now.isoformat(),
    }
    if not raw:
        payload["ok"] = False
        payload["reason"] = "empty_tool_args"
        payload["note"] = "Pass the caller's date phrase. Do not guess."
        return payload

    lowered = raw.lower()
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
