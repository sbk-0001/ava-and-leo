"""Pre-TTS grounding gate. SpeakableFacts come from successful tools only.

Realtime S2S has no TTS node — intercept lives on:
  * session.say / SessionSpeaker (fillers, scripts) — true pre-speech
  * Agent.tts_node — STT-LLM-TTS pipeline
  * Agent.transcription_node — Realtime transcript rewrite + interrupt/say

Docs: https://docs.livekit.io/agents/logic/nodes/
      https://docs.livekit.io/agents/multimodality/audio/#session-say
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from persona import BRANCHES, get_branch

logger = logging.getLogger("ava.grounding")

SYDNEY = ZoneInfo("Australia/Sydney")

SAFE_SUBSTITUTE = (
    "Hang on, let me check that properly — I don't want to give you the wrong time."
)
CONFIRM_SUBSTITUTE = "That's not locked yet. Let me have another look."
DATE_SUBSTITUTE = "Sorry, which day did you mean? I don't want to guess."
CHOCKERS_SUBSTITUTE = "I couldn't get a clean look at the diary just then."

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_HOUR_WORDS = {
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
_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    21: "twenty-first",
    22: "twenty-second",
    23: "twenty-third",
    24: "twenty-fourth",
    25: "twenty-fifth",
    26: "twenty-sixth",
    27: "twenty-seventh",
    28: "twenty-eighth",
    29: "twenty-ninth",
    30: "thirtieth",
    31: "thirty-first",
}

_TIME_RE = re.compile(
    r"\b(?:half\s+past|quarter\s+past|quarter\s+to|ten\s+past|twenty\s+past|"
    r"twenty\s+to|ten\s+to|\d{1,2}:\d{2}|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s+(?:o'clock|thirty|forty-five|fifteen|ten|twenty))\b",
    re.I,
)
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_DATE_ISO_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_ORDINAL_RE = re.compile(
    r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b",
    re.I,
)
_CONFIRM_RE = re.compile(
    r"\b(?:you'?re\s+all\s+set|all\s+set|confirmed|you'?re\s+booked|"
    r"booked\s+you|locked\s+in|booking'?s\s+confirmed|i'?ve\s+booked)\b",
    re.I,
)
_CHOCKERS_RE = re.compile(
    r"\b(?:chockers|fully\s+booked|nothing(?:\s+at\s+all)?\s+"
    r"(?:this|next)\s+week|packed\s+(?:this|next)\s+week)\b",
    re.I,
)
_WEEKDAY_RE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_DR_RE = re.compile(r"\bdr\.?\s+[a-z]+(?:\s+[a-z]+)?", re.I)


@dataclass
class SpeakableFacts:
    """Facts Ava may speak. Filled only from successful tool results."""

    dates: set[str] = field(default_factory=set)
    times: set[str] = field(default_factory=set)
    dentists: set[str] = field(default_factory=set)
    dentist_display_name: str | None = None
    confirm_allowed: bool = False
    availability_status: str | None = None
    slots_empty: bool = False
    date_resolved: bool = False

    @property
    def may_say_chockers(self) -> bool:
        return self.availability_status == "OK" and self.slots_empty

    def allow_calendar(self, today: date) -> None:
        """Today/tomorrow are calendar facts, not diary offers."""
        tomorrow = today.fromordinal(today.toordinal() + 1)
        self.dates.update(_date_variants(today))
        self.dates.update(_date_variants(tomorrow))
        self.dates.update({"today", "tomorrow", "this arvo", "this morning"})


@dataclass
class GateResult:
    original: str
    spoken: str
    suppressed: bool
    violations: list[str] = field(default_factory=list)

    @property
    def log_line(self) -> str:
        kinds = ",".join(self.violations) or "none"
        return (
            f"GROUNDING_VIOLATION kinds={kinds} "
            f"original={self.original!r} spoken={self.spoken!r}"
        )


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def spoken_time_variants(hhmm: str) -> set[str]:
    raw = (hhmm or "").strip()
    out = {raw, _norm(raw)}
    try:
        hour_s, minute_s = raw.split(":")[:2]
        hour = int(hour_s)
        minute = int(minute_s)
    except (TypeError, ValueError):
        return {item for item in out if item}
    hour12 = hour % 12 or 12
    hour_word = _HOUR_WORDS.get(hour12, str(hour12))
    out.update(
        {
            f"{hour}:{minute:02d}",
            f"{hour12}:{minute:02d}",
            hour_word,
            f"{hour12:02d}:{minute:02d}",
        }
    )
    if minute == 0:
        out.update({f"{hour_word} o'clock", f"{hour_word} oclock"})
    elif minute == 10:
        out.add(f"ten past {hour_word}")
    elif minute == 15:
        out.add(f"quarter past {hour_word}")
    elif minute == 20:
        out.add(f"twenty past {hour_word}")
    elif minute == 30:
        out.add(f"half past {hour_word}")
    elif minute == 40:
        nxt = _HOUR_WORDS.get(hour12 % 12 + 1, "")
        if nxt:
            out.add(f"twenty to {nxt}")
    elif minute == 45:
        nxt = _HOUR_WORDS.get(hour12 % 12 + 1, "")
        out.add(f"{hour_word} forty-five")
        if nxt:
            out.add(f"quarter to {nxt}")
    return {_norm(item) for item in out if item}


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _format_sydney_date(day: date) -> str:
    return f"{day.strftime('%A')} the {_ordinal(day.day)} of {day.strftime('%B %Y')}"


def _named_dentists() -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for branch in BRANCHES.values():
        for name in (*branch.dentists, *(c.name for c in branch.clinicians)):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
    return tuple(names)


def _date_variants(day: date) -> set[str]:
    spoken = _format_sydney_date(day)
    weekday = day.strftime("%A")
    month = day.strftime("%B")
    ordinal_word = _ORDINALS.get(day.day, f"{day.day}th")
    ordinal = _ordinal(day.day)
    return {
        _norm(day.isoformat()),
        _norm(weekday),
        _norm(month),
        _norm(spoken),
        _norm(ordinal),
        _norm(ordinal_word),
        _norm(f"the {ordinal}"),
        _norm(f"{ordinal} of {month}"),
        _norm(f"{weekday} the {ordinal}"),
        _norm(f"{weekday} the {ordinal} of {month}"),
        str(day.day),
    }


def dentist_variants(name: str) -> set[str]:
    text = (name or "").strip()
    if not text:
        return set()
    out = {_norm(text), text}
    bare = re.sub(r"^dr\.?\s+", "", text, flags=re.I).strip()
    out.add(_norm(bare))
    out.add(_norm(f"dr {bare}"))
    out.add(_norm(f"dr. {bare}"))
    parts = bare.split()
    if parts:
        out.add(_norm(parts[0]))
        out.add(_norm(parts[-1]))
        out.add(_norm(f"dr {parts[0]}"))
        out.add(_norm(f"dr {parts[-1]}"))
    return {item for item in out if item}


def _ingest_slot(facts: SpeakableFacts, slot: Mapping[str, Any]) -> None:
    day_raw = str(slot.get("date") or "")
    try:
        day = date.fromisoformat(day_raw)
        facts.dates.update(_date_variants(day))
    except ValueError:
        if day_raw:
            facts.dates.add(_norm(day_raw))
    time_raw = str(slot.get("time") or "")
    if time_raw:
        facts.times.update(spoken_time_variants(time_raw))
    clinician = str(slot.get("clinician") or "").strip()
    if clinician:
        facts.dentists.update(dentist_variants(clinician))
        facts.dentist_display_name = facts.dentist_display_name or clinician


def ingest_availability(
    facts: SpeakableFacts,
    result: Mapping[str, Any],
    *,
    today: date | None = None,
) -> SpeakableFacts:
    facts.allow_calendar(today or datetime.now(SYDNEY).date())
    status = str(result.get("status") or "")
    if status:
        facts.availability_status = status
    elif result.get("ok"):
        facts.availability_status = "OK"
    else:
        facts.availability_status = "UNKNOWN"
    if not result.get("ok"):
        facts.slots_empty = True
        return facts
    slots = list(result.get("slots") or [])
    facts.slots_empty = not slots
    facts.date_resolved = True
    for slot in slots:
        if isinstance(slot, Mapping):
            _ingest_slot(facts, slot)
    return facts


def ingest_book_result(
    facts: SpeakableFacts,
    result: Mapping[str, Any],
    *,
    today: date | None = None,
) -> SpeakableFacts:
    facts.allow_calendar(today or datetime.now(SYDNEY).date())
    locked = bool(result.get("ok") and result.get("confirmed"))
    facts.confirm_allowed = locked
    if not locked:
        return facts
    _ingest_slot(facts, result)
    return facts


def ingest_date_resolution(
    facts: SpeakableFacts,
    result: Mapping[str, Any],
    *,
    today: date | None = None,
) -> SpeakableFacts:
    facts.allow_calendar(today or datetime.now(SYDNEY).date())
    if result.get("ambiguous") or not result.get("resolved"):
        facts.date_resolved = False
        return facts
    facts.date_resolved = True
    for key in ("start", "end"):
        raw = str(result.get(key) or "")
        try:
            facts.dates.update(_date_variants(date.fromisoformat(raw)))
        except ValueError:
            continue
    spoken = str(result.get("spoken") or "")
    if spoken:
        facts.dates.add(_norm(spoken))
    return facts


def _known(token: str, allowed: set[str]) -> bool:
    needle = _norm(token)
    if needle in allowed:
        return True
    return any(needle in item or item in needle for item in allowed if len(item) > 2)


def _pin_dentist(text: str, facts: SpeakableFacts) -> tuple[str, bool]:
    display = facts.dentist_display_name
    if not display:
        return text, False
    swapped = False
    known = {item for item in facts.dentists if item}

    def _replace(match: re.Match[str]) -> str:
        nonlocal swapped
        spoken = match.group(0)
        if _known(spoken, known):
            return spoken
        swapped = True
        return display

    rewritten = _DR_RE.sub(_replace, text)
    if not swapped:
        for name in _named_dentists():
            if name.lower() == display.lower():
                continue
            if name.lower() in rewritten.lower() and not _known(name, known):
                rewritten = re.sub(re.escape(name), display, rewritten, flags=re.I)
                swapped = True
    return rewritten, swapped


def gate_utterance(text: str, facts: SpeakableFacts) -> GateResult:
    """Strip ungrounded date/time/dentist/confirm language before TTS."""
    original = text
    spoken = text
    violations: list[str] = []
    if not original.strip():
        return GateResult(original=original, spoken=spoken, suppressed=False)

    if _CONFIRM_RE.search(spoken) and not facts.confirm_allowed:
        violations.append("confirm")
        result = GateResult(
            original=original,
            spoken=CONFIRM_SUBSTITUTE,
            suppressed=True,
            violations=violations,
        )
        logger.warning("%s", result.log_line)
        return result

    if _CHOCKERS_RE.search(spoken) and not facts.may_say_chockers:
        violations.append("chockers")
        result = GateResult(
            original=original,
            spoken=CHOCKERS_SUBSTITUTE,
            suppressed=True,
            violations=violations,
        )
        logger.warning("%s", result.log_line)
        return result

    spoken, dentist_swapped = _pin_dentist(spoken, facts)
    if dentist_swapped:
        violations.append("dentist")

    date_hits = [match.group(0) for match in _WEEKDAY_RE.finditer(spoken)]
    date_hits.extend(match.group(0) for match in _DATE_ISO_RE.finditer(spoken))
    date_hits.extend(match.group(0) for match in _ORDINAL_RE.finditer(spoken))
    if date_hits and not facts.date_resolved:
        extra = [hit for hit in date_hits if not _known(hit, facts.dates)]
        if extra or not facts.dates:
            violations.append("date")
            result = GateResult(
                original=original,
                spoken=DATE_SUBSTITUTE
                if "next " in original.lower()
                else SAFE_SUBSTITUTE,
                suppressed=True,
                violations=violations,
            )
            logger.warning("%s", result.log_line)
            return result
    ungrounded_dates = [hit for hit in date_hits if not _known(hit, facts.dates)]
    if ungrounded_dates:
        violations.append("date")
        result = GateResult(
            original=original,
            spoken=SAFE_SUBSTITUTE,
            suppressed=True,
            violations=violations,
        )
        logger.warning("%s", result.log_line)
        return result

    time_hits = [match.group(0) for match in _TIME_RE.finditer(spoken)]
    time_hits.extend(match.group(0) for match in _CLOCK_RE.finditer(spoken))
    ungrounded_times = [hit for hit in time_hits if not _known(hit, facts.times)]
    if ungrounded_times:
        violations.append("time")
        result = GateResult(
            original=original,
            spoken=SAFE_SUBSTITUTE,
            suppressed=True,
            violations=violations,
        )
        logger.warning("%s", result.log_line)
        return result

    suppressed = bool(violations) or spoken != original
    result = GateResult(
        original=original,
        spoken=spoken,
        suppressed=suppressed,
        violations=violations,
    )
    if suppressed:
        logger.warning("%s", result.log_line)
    return result


def grounding_violation_packet(
    gated: GateResult,
    *,
    count: int,
    branch: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "violations": list(gated.violations),
        "original": gated.original[:240],
        "spoken": gated.spoken,
        "count": count,
        "ok": False,
    }
    if branch:
        payload["branch"] = get_branch(branch).id
    return {
        "type": "grounding_violation",
        "action": "grounding_violation",
        "label": "GROUNDING_VIOLATION",
        "payload": payload,
        "refresh_diary": False,
    }
