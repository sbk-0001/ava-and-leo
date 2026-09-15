"""Drop junk / background ASR so Ava does not treat it as a caller turn.

Live calls mixed in TV/other-room fragments (non-English scraps, "mm",
"Load what?" echo of her own filler) which stole the goal and the name.
This is code, not a prompt hint.

Docs: https://docs.livekit.io/agents/logic/turns/
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TASK_RE = re.compile(
    r"(?i)\b("
    r"book|booking|cancel|reschedule|appointment|dentist|doctor|dr\.?|"
    r"name|birth|dob|tomorrow|today|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|check-?up|tooth|pain|mobile|number|"
    r"time|date|slot|available|mohit|johnson|verify|confirmed"
    r")\b"
)
BARGE_RE = re.compile(
    r"(?i)\b("
    r"stop|wait|hang on|hold on|hello|hiya|"
    r"are you (?:there|doing it|still there|looking)|"
    r"excuse me|cancel that|don't|do not"
    r")\b"
)
FILLER_ONLY_RE = re.compile(
    r"^(?:m+h?m+|u+h+|u+m+|a+h+|o+h+|yeah|yep|yup|nah|"
    r"ok(?:ay)?|iya|hmm+|huh|mm+|mhm+|huh\??)\s*[.!?]*$",
    re.I,
)
NAME_CORRECTION_RE = re.compile(
    r"(?i)\b(?:my name(?:'?s| is)|(?:it'?s|this is) actually|"
    r"the name is|name is|call me|i(?:'?m| am))\s+"
    r"([A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,2})"
)
_NAME_BLOCKLIST = frozenset(
    {
        "ava",
        "actually",
        "calling",
        "coming",
        "here",
        "looking",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "today",
        "tomorrow",
        "not",
        "the",
        "this",
        "that",
    }
)
_FOREIGN_HINTS = (
    "puedes",
    "buscar",
    "mundo",
    "güzel",
    "guzel",
    "ne güzel",
    "iya",
)
IN_FLIGHT_SHORT_CHARS = 12
LOW_CONFIDENCE = 0.45


@dataclass(frozen=True)
class TurnVerdict:
    ignore: bool
    reason: str
    barge: bool = False
    task: bool = False
    name_correction: str | None = None

    @property
    def affects_ladder(self) -> bool:
        return (not self.ignore) and (self.barge or self.task)


def extract_name_correction(text: str) -> str | None:
    match = NAME_CORRECTION_RE.search(text or "")
    if not match:
        return None
    name = re.sub(r"\s+", " ", match.group(1)).strip(" .,!?")
    if not name:
        return None
    tokens = name.split()
    if any(token.lower() in _NAME_BLOCKLIST for token in tokens):
        return None
    if len(name) < 2:
        return None
    return name


def _has_non_latin_letters(text: str) -> bool:
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" not in name:
            return True
    return False


def _echoes_filler(
    text: str, recent_fillers: list[str] | tuple[str, ...] | None
) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered or not recent_fillers:
        return False
    for line in recent_fillers:
        hay = (line or "").strip().lower()
        if not hay:
            continue
        if lowered == hay:
            return True
        if "load" in hay and "load" in lowered and len(lowered) < 24:
            return True
    return False


def classify_user_turn(
    text: str,
    *,
    tool_in_flight: bool = False,
    confidence: float | None = None,
    recent_fillers: list[str] | tuple[str, ...] | None = None,
) -> TurnVerdict:
    raw = (text or "").strip()
    if not raw:
        return TurnVerdict(True, "empty")
    name = extract_name_correction(raw)
    task = bool(TASK_RE.search(raw) or name)
    barge = bool(BARGE_RE.search(raw))
    if name:
        return TurnVerdict(
            False,
            "name_correction",
            barge=barge,
            task=True,
            name_correction=name,
        )
    if task:
        return TurnVerdict(False, "task", barge=barge, task=True)
    if FILLER_ONLY_RE.match(raw):
        return TurnVerdict(True, "filler_only", barge=False, task=False)
    if _echoes_filler(raw, recent_fillers):
        return TurnVerdict(True, "filler_echo")
    lowered = raw.lower()
    if any(hint in lowered for hint in _FOREIGN_HINTS) or _has_non_latin_letters(raw):
        return TurnVerdict(True, "non_task_language")
    if confidence is not None and confidence < LOW_CONFIDENCE:
        return TurnVerdict(True, "low_confidence")
    if tool_in_flight:
        if barge:
            return TurnVerdict(False, "barge", barge=True, task=False)
        if len(raw) < IN_FLIGHT_SHORT_CHARS or len(raw.split()) <= 2:
            return TurnVerdict(True, "in_flight_fragment")
        return TurnVerdict(True, "in_flight_non_task")
    if len(raw) <= 2:
        return TurnVerdict(True, "too_short")
    return TurnVerdict(False, "ok", barge=barge, task=False)
