"""CallState is the single source of truth for Ava's flow.

Prompt-only counters have failed twice. Ask loops, branch identity, urgency and
escalation live here and are injected into the session before Ava speaks.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from booking_state import BookingState
from date_context import DateContext, refresh_date_context
from grounding import (
    GateResult,
    SpeakableFacts,
    gate_utterance,
    ingest_availability,
    ingest_book_result,
    ingest_clock_fact,
    ingest_date_resolution,
)
from persona import BRANCHES, DEFAULT_BRANCH_ID, GROUP_TRADING_NAME, get_branch
from phrase_pools import ACKS, BARGE_IN_RESUME, CLOSINGS, OPENINGS, pick_from_pool
from turn_filter import extract_name_correction

MAX_ASKS = 3
MAX_DOB_ATTEMPTS = 2

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

DOB_RETRY_NOTE = (
    "They may volunteer another date of birth once. Call verify_date_of_birth "
    "again. Do not tell them the date of birth did not match. "
    "Do not confirm or deny a record."
)
DOB_CALLBACK_NOTE = (
    "Offer to have the team call back. Do not mention date of birth. "
    "Do not confirm or deny a record."
)
DOB_RETRY_SAY = (
    "I just need to confirm your date of birth once more, whenever you're ready."
)
DOB_CALLBACK_SAY = (
    "I'll get the team to give you a call back. I can't confirm that from here."
)
BOOK_NEED_NAME_SAY = "I just need the name for the booking."
BOOK_NEED_MOBILE_SAY = "What's the best mobile for that booking?"
BOOK_NEED_BOTH_SAY = "I just need a name and a mobile for the booking."

UrgencyLevel = Literal["routine", "same_day", "emergency_000"]

# Suburbs we will offer another branch for — never a "which branch" menu.
# Figtree is the Thursday demo highlight: offer Dapto.
SUBURB_OFFERS: dict[str, str] = {
    "figtree": "dapto",
    "unanderra": "dapto",
    "kembla grange": "dapto",
    "berkeley": "dapto",
    "thirroul": "woonona",
    "bulli": "woonona",
    "corrimal": "woonona",
    "austinmer": "woonona",
    "bellambi": "woonona",
    "towradgi": "woonona",
    "barrack heights": "shellharbour",
    "warilla": "shellharbour",
    "flinders": "shellharbour",
    "shellharbour": "shellharbour",
    "oak flats": "shellharbour",
    "mount warrigal": "shellharbour",
}

SYDNEY = ZoneInfo("Australia/Sydney")
_MOBILE_RE = re.compile(r"^(?:\+?61|0)4\d{8}$")
_DR_NAME_RE = re.compile(r"\bdr\.?\s+([a-z]+(?:\s+[a-z]+)?)", re.I)

NOT_LOCKED_SAY = (
    "This booking is not locked yet. Do not say you're all set, confirmed, "
    "booked, or similar. Say it is not locked yet and offer another slot "
    "from check_availability, or try again."
)
LOCKED_SAY = (
    "Booking is locked. Confirm name, weekday, date, time and dentist once. "
    "You may say they're all set."
)

_LIFE_THREATENING = (
    "can't swallow",
    "cannot swallow",
    "trouble swallowing",
    "difficulty swallowing",
    "can't breathe",
    "cannot breathe",
    "trouble breathing",
    "difficulty breathing",
    "airway",
    "spreading toward the eye",
    "down the neck",
    "uncontrolled bleeding",
    "won't stop bleeding",
    "head trauma",
    "facial trauma",
    "knocked out cold",
    "unconscious",
)

_SAME_DAY = (
    "knocked out",
    "knocked-out",
    "keeping me awake",
    "can't sleep",
    "abscess",
    "swollen",
    "swelling",
    "broken tooth",
    "sharp pain",
    "wisdom",
)


def kill_switch_enabled(env: Mapping[str, str] | None = None) -> bool:
    """AVA_KILL_SWITCH routes straight to a human. Demo safety."""
    environ = env if env is not None else os.environ
    raw = str(environ.get("AVA_KILL_SWITCH", "")).strip().lower()
    return raw in {"1", "true", "yes", "on", "transfer"}


def normalize_dob(value: str | None) -> str:
    """Return YYYY-MM-DD from ISO, numeric, or spoken Australian dates."""
    raw = (value or "").strip()
    if not raw:
        return ""
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if iso:
        try:
            return date(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
            ).isoformat()
        except ValueError:
            return ""
    lowered = raw.lower()
    month: int | None = None
    for name, number in sorted(_MONTHS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            month = number
            break
    nums = [int(token) for token in re.findall(r"\d+", raw)]
    if month is not None:
        day = next((n for n in nums if 1 <= n <= 31), None)
        year = next((n for n in nums if n >= 1900), None)
        if day and year:
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return ""
    if len(nums) >= 3:
        day, month_n, year = nums[0], nums[1], nums[2]
        if year < 100:
            year += 1900 if year >= 30 else 2000
        try:
            return date(year, month_n, day).isoformat()
        except ValueError:
            pass
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        yyyy_mm_dd = (int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        dd_mm_yyyy = (int(digits[4:8]), int(digits[2:4]), int(digits[0:2]))
        for year, month_n, day in (yyyy_mm_dd, dd_mm_yyyy):
            try:
                return date(year, month_n, day).isoformat()
            except ValueError:
                continue
    return ""


def is_valid_au_mobile(value: str | None) -> bool:
    if not value:
        return False
    digits = re.sub(r"\D", "", value)
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    return bool(_MOBILE_RE.match(digits if digits.startswith("0") else "0" + digits))


def offer_branch_for_suburb(suburb: str, current_branch: str) -> str | None:
    """If the suburb clearly suits another clinic, return that branch id."""
    key = re.sub(r"\s+", " ", suburb.strip().lower())
    offered = SUBURB_OFFERS.get(key)
    if offered and offered != current_branch:
        return offered
    return None


def sydney_today(now: date | None = None) -> date:
    return now or datetime.now(SYDNEY).date()


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_sydney_date(day: date) -> str:
    """Spoken Australia/Sydney date, e.g. Tuesday the 15th of September 2026."""
    return f"{day.strftime('%A')} the {_ordinal(day.day)} of {day.strftime('%B %Y')}"


def format_spoken_dob(value: str) -> str | None:
    """Spoken DOB without weekday, e.g. the 15th of January 1990."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        day = date.fromisoformat(raw[:10])
    except ValueError:
        return raw
    return f"the {_ordinal(day.day)} of {day.strftime('%B %Y')}"


def named_dentists() -> tuple[str, ...]:
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


def match_clinician(text: str) -> str | None:
    """Optional clinician filter when the caller names a dentist."""
    if not text:
        return None
    lowered = text.lower()
    dentists = named_dentists()
    for name in dentists:
        if name.lower() in lowered:
            return name
        bare = re.sub(r"^dr\.?\s+", "", name.lower())
        parts = [part for part in bare.split() if len(part) > 3]
        if any(part in lowered for part in parts):
            return name
    match = _DR_NAME_RE.search(text)
    if not match:
        return None
    token = match.group(1).lower()
    for name in dentists:
        if token in name.lower():
            return name
    return match.group(0).strip()


def booking_is_locked(result: Mapping[str, Any] | None) -> bool:
    if not result:
        return False
    return bool(result.get("ok") and result.get("confirmed"))


def apply_confirmation_gate(result: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp a book/reschedule result so Ava cannot verbally confirm a miss."""
    payload = dict(result)
    if booking_is_locked(payload):
        payload.setdefault("say", LOCKED_SAY)
        return payload
    payload["confirmed"] = False
    payload["say"] = NOT_LOCKED_SAY
    return payload


def classify_urgency(text: str) -> UrgencyLevel:
    lowered = text.lower()
    if any(needle in lowered for needle in _LIFE_THREATENING):
        return "emergency_000"
    if any(needle in lowered for needle in _SAME_DAY):
        return "same_day"
    return "routine"


@dataclass
class AskResult:
    field: str
    count: int
    stop_asking: bool
    offer_callback: bool


@dataclass
class CallState:
    branch: str = DEFAULT_BRANCH_ID
    caller_name: str | None = None
    caller_mobile: str | None = None
    is_existing_patient: bool | None = None
    intent: str | None = None
    appointment_type: str | None = None
    proposed_slot: str | None = None
    confirmed_slot: str | None = None
    urgency_level: UrgencyLevel = "routine"
    escalation_flag: bool = False
    turn_count: int = 0
    ask_counts: dict[str, int] = field(default_factory=dict)
    offered_branch: str | None = None
    bot_ask_count: int = 0
    kill_switch: bool = False
    greet_on_enter: bool = True
    used_phrases: dict[str, list[str]] = field(default_factory=dict)
    stock_phrases_used: list[str] = field(default_factory=list)
    last_dispatch_trace: Any | None = None
    last_availability_slots: list[dict[str, Any]] = field(default_factory=list)
    last_book_result: dict[str, Any] | None = None
    preferred_clinician: str | None = None
    today: date = field(default_factory=sydney_today)
    now: datetime | None = None
    clock_frozen: bool = False
    date_context: DateContext = field(init=False)
    speakable: SpeakableFacts = field(default_factory=SpeakableFacts)
    booking_flow: BookingState = field(default_factory=BookingState)
    grounding_violations: int = 0
    barge_in_pending: bool = False
    last_barge_in_resume: str | None = None
    phrase_rng: random.Random = field(default_factory=random.Random)
    known_caller: bool = False
    ani: str | None = None
    channel: str = "unknown"
    dob_verified: bool = False
    dob_failed: bool = False
    dob_attempts: int = 0
    preferred_branch: str | None = None
    usual_dentist: str | None = None
    last_appointment_private: dict[str, Any] | None = None
    pms_record: dict[str, Any] | None = None
    pending_grounding_note: str | None = None
    name_corrected: bool = False
    verified_dob: str | None = None
    verified_dob_spoken: str | None = None
    active_goal: str | None = None
    goal_kind: str | None = None
    junk_turns: int = 0
    book_confirm_kicked_at: float | None = None

    def __post_init__(self) -> None:
        live = datetime.now(SYDNEY)
        if self.now is not None:
            if self.now.tzinfo is None:
                self.now = self.now.replace(tzinfo=SYDNEY)
            else:
                self.now = self.now.astimezone(SYDNEY)
            self.today = self.now.date()
            self.clock_frozen = True
        elif self.today == live.date():
            self.now = live
        else:
            self.now = datetime.combine(self.today, datetime.min.time(), tzinfo=SYDNEY)
            self.clock_frozen = True
        self.date_context = refresh_date_context(now=self.now)
        self.speakable.allow_calendar(self.today)

    def refresh_dates(
        self,
        today: date | None = None,
        now: datetime | None = None,
    ) -> DateContext:
        """Recompute Sydney DateContext every turn, including the live clock."""
        if now is not None:
            if now.tzinfo is None:
                now = now.replace(tzinfo=SYDNEY)
            else:
                now = now.astimezone(SYDNEY)
            self.now = now
            self.today = now.date()
            self.clock_frozen = True
        elif today is not None:
            self.today = today
            clock = self.now or datetime.now(SYDNEY)
            self.now = datetime.combine(today, clock.timetz().replace(tzinfo=SYDNEY))
            self.clock_frozen = True
        elif self.clock_frozen:
            self.date_context = refresh_date_context(now=self.now)
            self.speakable.allow_calendar(self.today)
            return self.date_context
        else:
            self.now = datetime.now(SYDNEY)
            self.today = self.now.date()
        self.date_context = refresh_date_context(now=self.now)
        self.speakable.allow_calendar(self.today)
        return self.date_context

    @property
    def tomorrow(self) -> date:
        return self.today + timedelta(days=1)

    @property
    def today_spoken(self) -> str:
        return format_sydney_date(self.today)

    @property
    def tomorrow_spoken(self) -> str:
        return format_sydney_date(self.tomorrow)

    def remember_availability(self, result: Mapping[str, Any]) -> None:
        """Keep the last diary result so a TPM recovery can offer times, not re-search."""
        ingest_availability(self.speakable, result, today=self.today)
        slots = list(result.get("slots") or []) if result.get("ok") else []
        self.last_availability_slots = slots[:8]
        if slots:
            self.proposed_slot = slots[0].get("slot_id") or self.proposed_slot
            first = slots[0] if isinstance(slots[0], dict) else {}
            clinician = str(first.get("clinician") or "").strip()
            if clinician:
                self.speakable.dentist_display_name = clinician
        self.booking_flow.offer_slots(slots if result.get("ok") else [])

    def record_book_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Gate verbal confirmation on the last book_appointment result."""
        gated = apply_confirmation_gate(result)
        self.last_book_result = gated
        ingest_book_result(self.speakable, gated, today=self.today)
        if booking_is_locked(gated):
            slot_id = gated.get("slot_id")
            if slot_id:
                self.confirmed_slot = str(slot_id)
            self.intent = "booked"
            self.booking_flow.confirm(str(slot_id) if slot_id else None)
        else:
            reason = str(gated.get("reason") or "not_locked")
            callback = None
            if self.may_offer_callback():
                callback = {"action": "take_message", "reason": reason}
            self.booking_flow.fail(reason, callback_task=callback)
            self.speakable.confirm_allowed = False
        return gated

    def apply_date_resolution(self, result: Mapping[str, Any]) -> None:
        ingest_date_resolution(self.speakable, result, today=self.today)

    def apply_clock_fact(self, result: Mapping[str, Any]) -> None:
        """Let Ava speak the clock she just looked up."""
        ingest_clock_fact(self.speakable, result, today=self.today)

    def gate_speech(self, text: str) -> GateResult:
        gated = gate_utterance(text, self.speakable)
        if gated.suppressed:
            self.grounding_violations += 1
            from grounding import grounding_corrective_note

            self.pending_grounding_note = grounding_corrective_note(
                gated.original, gated.violations
            )
        return gated

    def known_value(self, field: str) -> str | None:
        key = (field or "").strip().lower()
        if key in {"mobile", "phone", "number"}:
            return self.caller_mobile
        if key in {"name", "caller_name"}:
            return self.caller_name
        if key in {"branch"}:
            return self.branch
        if key in {"dob", "date_of_birth"}:
            if self.dob_verified and self.verified_dob:
                return self.verified_dob
            return "verified" if self.dob_verified else None
        return None

    def ask_for(self, field: str) -> dict[str, Any]:
        """Hard-reject a re-ask when the field is already populated."""
        known = self.known_value(field)
        if known:
            return {
                "ok": False,
                "already_known": True,
                "field": field,
                "value": known,
                "note": f"already known: {known}",
            }
        result = self.record_ask(field)
        return {
            "ok": True,
            "already_known": False,
            "field": field,
            "attempts": result.count,
            "stop_asking": result.stop_asking,
            "note": "Ask once, then store the answer. Do not ask again if they give it.",
        }

    def known_facts_block(self) -> str:
        """Compact facts injected every turn. Clinical detail is redacted until DOB."""
        mobile = self.caller_mobile or "unknown"
        name = self.caller_name or "unknown"
        dentist = (
            self.preferred_clinician or self.usual_dentist
            if self.dob_verified
            else "(redacted until DOB verified)"
        )
        last_appt = "none"
        if self.dob_verified and self.last_appointment_private:
            last = self.last_appointment_private
            last_appt = (
                f"{last.get('date') or ''} {last.get('time') or ''}".strip()
                or "on file"
            )
        elif self.last_appointment_private or (self.pms_record or {}).get("bookings"):
            last_appt = "(redacted until DOB verified)"
        patient_status = (
            str(self.is_existing_patient)
            if self.dob_verified
            else "unverified — do not confirm or deny"
        )
        dob_line = "hidden"
        dob_rule = "Never disclose a date of birth. Never volunteer DOB unprompted."
        if self.dob_verified and self.verified_dob:
            spoken = self.verified_dob_spoken or format_spoken_dob(self.verified_dob)
            dob_line = f"{spoken} (ISO {self.verified_dob})"
            dob_rule = (
                "If they ask for the DOB they just verified / on file for "
                "themselves, read verified_dob back once. Never volunteer it "
                "unprompted."
            )
        return (
            "KNOWN FACTS (already collected — never ask again):\n"
            f"- first_name: {self.caller_first_name or 'unknown'}\n"
            f"- caller_name: {name}\n"
            f"- name_corrected: {self.name_corrected}\n"
            f"- caller_mobile: {mobile}\n"
            f"- known_caller: {self.known_caller}\n"
            f"- ani: {self.ani or 'none'}\n"
            f"- dob_verified: {self.dob_verified}\n"
            f"- verified_dob: {dob_line}\n"
            f"- dob_attempts: {self.dob_attempts}/{MAX_DOB_ATTEMPTS}\n"
            f"- is_existing_patient: {patient_status}\n"
            f"- preferred_branch: {self.preferred_branch or self.branch}\n"
            f"- usual_dentist: {dentist}\n"
            f"- last_appointment: {last_appt}\n"
            f"- active_goal: {self.active_goal or 'none'}\n"
            f"- goal_kind: {self.goal_kind or 'none'}\n"
            "- Soft ANI greet name is only a guess until name_corrected is true. "
            "If name_corrected, always use caller_name — never the store greet.\n"
            "- If caller_mobile is set: do not ask for their number. "
            "You may light-confirm 'Is this still the best number for ya?'\n"
            "- New booking: no DOB required. Discuss/move/cancel existing: DOB required.\n"
            "- Do not volunteer existing appointment, dentist, or treatment detail "
            "until dob_verified is true.\n"
            f"- {dob_rule}\n"
            "- First failed DOB: they may volunteer another date — call "
            "verify_date_of_birth once more. Do not say it was wrong.\n"
            "- Second failed DOB: offer a callback. Do not mention date of birth. "
            "Do not confirm or deny a record.\n"
            "- Ignore background chatter, non-English scraps, and mm/mhm/yeah "
            "while a tool is in flight. Only act on clear booking, cancel, "
            "reschedule, or identity intent. Side noise must not clear active_goal.\n"
            "- After a tool returns, resume active_goal. Never wander into small "
            "talk about the store name."
        )

    @property
    def caller_first_name(self) -> str | None:
        if not self.caller_name:
            return None
        token = self.caller_name.strip().split()[0]
        return token or None

    def correct_caller_name(self, name: str | None) -> dict[str, Any]:
        """In-call name correction wins over ANI greet and the caller store."""
        cleaned = re.sub(r"\s+", " ", (name or "").strip())
        if not cleaned:
            return {"ok": False, "reason": "empty_name"}
        self.caller_name = cleaned
        self.name_corrected = True
        return {
            "ok": True,
            "name": cleaned,
            "first_name": self.caller_first_name,
            "name_corrected": True,
            "note": (
                f"Use {cleaned} from now on. The ANI greet name is stale. "
                "KnownFacts and this booking must use this name."
            ),
        }

    def lock_goal(
        self,
        kind: str,
        summary: str,
        *,
        replace: bool = True,
    ) -> None:
        if not kind or not summary:
            return
        if self.goal_kind and not replace:
            return
        if self.goal_kind == "cancel" and kind == "book":
            self.goal_kind = "cancel_then_book"
            self.active_goal = f"{self.active_goal}; then {summary}"
            return
        self.goal_kind = kind
        self.active_goal = summary

    def dob_readback(self) -> dict[str, Any]:
        if not self.dob_verified or not self.verified_dob:
            return {
                "ok": False,
                "reason": "not_verified",
                "note": (
                    "Do not read back or disclose a date of birth. "
                    "Identity is not verified on this call."
                ),
            }
        spoken = self.verified_dob_spoken or format_spoken_dob(self.verified_dob)
        return {
            "ok": True,
            "date_of_birth": self.verified_dob,
            "spoken": spoken,
            "note": (
                "They asked for their own verified date of birth. "
                "Read it back once. Do not volunteer it again."
            ),
        }

    def may_disclose_existing(self) -> bool:
        return self.dob_verified and not self.dob_failed

    def require_dob_for_existing(self) -> dict[str, Any]:
        if self.may_disclose_existing():
            return {"ok": True, "verified": True}
        if self.dob_failed:
            return {
                "ok": False,
                "reason": "verification_failed",
                "retry_allowed": False,
                "say": DOB_CALLBACK_SAY,
                "note": DOB_CALLBACK_NOTE,
            }
        payload: dict[str, Any] = {
            "ok": False,
            "reason": "dob_required",
            "note": (
                "Need date of birth before discussing, moving, or cancelling an "
                "existing appointment. Ask once for DOB. Do not mention any "
                "existing booking details."
            ),
        }
        if self.dob_attempts:
            payload["retry_allowed"] = True
            payload["say"] = DOB_RETRY_SAY
            payload["note"] = DOB_RETRY_NOTE
        return payload

    def missing_booking_identity(
        self,
        *,
        name: str | None = None,
        mobile: str | None = None,
        patient_id: str | None = None,
    ) -> list[str]:
        if (patient_id or "").strip():
            return []
        need: list[str] = []
        if not (name or self.caller_name or "").strip():
            need.append("name")
        if not is_valid_au_mobile(mobile or self.caller_mobile):
            need.append("mobile")
        return need

    def booking_need_fields_result(self, need: list[str]) -> dict[str, Any]:
        if need == ["name", "mobile"]:
            say = BOOK_NEED_BOTH_SAY
        elif "name" in need:
            say = BOOK_NEED_NAME_SAY
        else:
            say = BOOK_NEED_MOBILE_SAY
        return {
            "ok": False,
            "confirmed": False,
            "reason": "need_fields",
            "need_fields": list(need),
            "say": say,
            "note": (
                "Do not call book_appointment again until these fields are collected. "
                "Ask now. Do not sit in silence."
            ),
        }

    def verify_dob(self, given: str | None) -> dict[str, Any]:
        expected = ""
        record = self.pms_record if isinstance(self.pms_record, dict) else {}
        patients = (
            record.get("patients") if isinstance(record.get("patients"), list) else []
        )
        patient = patients[0] if patients and isinstance(patients[0], dict) else None
        if patient:
            expected = str(patient.get("date_of_birth") or "")
        given_iso = normalize_dob(given)
        expected_iso = normalize_dob(expected)
        if given_iso and expected_iso and given_iso == expected_iso:
            return self._mark_dob_verified(given_iso, patient=patient, record=record)
        if given_iso and not expected_iso:
            # Nothing on file to check against, so there is nothing to fail. This
            # is the caller telling us their date of birth for the first time -
            # take it and remember it. Rejecting it asked them to "confirm" a
            # date we had never been given, which could never match.
            if patient is not None:
                patient["date_of_birth"] = given_iso
            return self._mark_dob_verified(given_iso, patient=patient, record=record)
        self.dob_attempts += 1
        self.dob_verified = False
        retry = self.dob_attempts < MAX_DOB_ATTEMPTS
        self.dob_failed = not retry
        return {
            "ok": False,
            "reason": "verification_failed",
            "retry_allowed": retry,
            "say": DOB_RETRY_SAY if retry else DOB_CALLBACK_SAY,
            "note": DOB_RETRY_NOTE if retry else DOB_CALLBACK_NOTE,
        }

    def _mark_dob_verified(
        self,
        stored_dob: str,
        *,
        patient: dict[str, Any] | None,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.dob_verified = True
        self.dob_failed = False
        self.verified_dob = stored_dob
        self.verified_dob_spoken = format_spoken_dob(stored_dob)
        if record.get("is_existing_patient"):
            self.is_existing_patient = True
        if patient is not None and stored_dob:
            patient["date_of_birth"] = stored_dob
        return {
            "ok": True,
            "verified": True,
            "stored_dob": stored_dob,
            "date_of_birth": stored_dob,
            "spoken": self.verified_dob_spoken,
            "note": (
                "Identity verified for this call. If they ask for this date "
                "of birth, you may read it back. Do not volunteer it."
            ),
        }

    def may_confirm_booking(self) -> bool:
        return booking_is_locked(self.last_book_result) or (
            self.booking_flow.confirm_language_allowed
        )

    def may_offer_callback(self) -> bool:
        """URGENT / EMERGENCY never offer a callback — stay on the line or 000."""
        return self.urgency_level not in {"same_day", "emergency_000"}

    def may_offer_times(self) -> bool:
        return bool(self.last_availability_slots)

    @property
    def branch_name(self) -> str:
        return get_branch(self.branch).trading_name

    def record_ask(self, field: str) -> AskResult:
        count = int(self.ask_counts.get(field, 0)) + 1
        self.ask_counts[field] = count
        stop = count >= MAX_ASKS
        return AskResult(
            field=field,
            count=count,
            stop_asking=stop,
            offer_callback=stop,
        )

    def should_stop_asking(self, field: str) -> bool:
        return int(self.ask_counts.get(field, 0)) >= MAX_ASKS

    def register_mobile(self, raw: str | None) -> dict[str, Any]:
        """Store a valid mobile, or increment the hard ask counter."""
        if (
            self.caller_mobile
            and is_valid_au_mobile(self.caller_mobile)
            and (not raw or is_valid_au_mobile(raw))
        ):
            return {
                "ok": True,
                "already_known": True,
                "mobile": self.caller_mobile,
                "stored": True,
                "note": f"already known: {self.caller_mobile}",
            }
        if self.should_stop_asking("mobile"):
            offer_callback = self.may_offer_callback()
            return {
                "ok": False,
                "stop_asking": True,
                "offer_callback": offer_callback,
                "attempts": self.ask_counts.get("mobile", MAX_ASKS),
                "note": (
                    "Do not ask for the mobile again. Offer a callback. "
                    "Take a message if you have a name."
                ),
            }
        if is_valid_au_mobile(raw):
            digits = re.sub(r"\D", "", raw or "")
            if digits.startswith("61"):
                digits = "0" + digits[2:]
            elif not digits.startswith("0"):
                digits = "0" + digits
            self.caller_mobile = digits
            return {"ok": True, "mobile": self.caller_mobile, "stored": True}

        result = self.record_ask("mobile")
        offer_callback = result.offer_callback and self.may_offer_callback()
        return {
            "ok": False,
            "stop_asking": result.stop_asking,
            "offer_callback": offer_callback,
            "attempts": result.count,
            "remaining": max(0, MAX_ASKS - result.count),
            "note": (
                "Do not ask for the mobile again. Offer a callback."
                if result.stop_asking
                else "Ask once more for the mobile, one question only."
            ),
        }

    def pick_phrase(
        self,
        pool: str,
        lines: Sequence[str],
        rng: random.Random | None = None,
    ) -> str:
        used = self.used_phrases.setdefault(pool, [])
        choice = pick_from_pool(used, lines, rng=rng or self.phrase_rng)
        self.record_stock_phrase(choice)
        return choice

    def record_stock_phrase(self, phrase: str) -> None:
        if phrase and phrase not in self.stock_phrases_used:
            self.stock_phrases_used.append(phrase)

    def pick_ack(self) -> str:
        return self.pick_phrase("ack", ACKS)

    def pick_opening(self, branch_name: str | None = None) -> str:
        name = branch_name or GROUP_TRADING_NAME
        used = self.used_phrases.setdefault("opening", [])
        template = pick_from_pool(used, OPENINGS, rng=self.phrase_rng)
        line = template.format(branch=name)
        self.record_stock_phrase(line)
        return line

    def pick_closing(self) -> str:
        return self.pick_phrase("closing", CLOSINGS)

    def mark_interrupted(self) -> str:
        """Caller cut Ava off. Resume with a pool line; never restart the sentence."""
        self.barge_in_pending = True
        phrase = self.pick_phrase("barge_in_resume", BARGE_IN_RESUME)
        self.last_barge_in_resume = phrase
        return phrase

    def consume_barge_in_resume(self) -> str | None:
        if not self.barge_in_pending:
            return None
        self.barge_in_pending = False
        return self.last_barge_in_resume

    def observe_user_text(self, text: str) -> None:
        """Code-side flow: suburb offers, urgency, bot asks. Never a branch menu."""
        if not text:
            return
        corrected = extract_name_correction(text)
        if corrected:
            self.correct_caller_name(corrected)
        lowered_goal = text.lower()
        if any(word in lowered_goal for word in ("cancel", "cancellation")):
            self.lock_goal("cancel", "Cancel the caller's existing appointment")
        if any(
            word in lowered_goal
            for word in ("reschedule", "move my appointment", "change my appointment")
        ):
            self.lock_goal("reschedule", "Reschedule the existing appointment")
        if any(
            word in lowered_goal
            for word in (
                "book",
                "check-up",
                "checkup",
                "new appointment",
                "make an appointment",
            )
        ):
            self.lock_goal("book", "Book a new appointment")
        urgency = classify_urgency(text)
        if urgency == "emergency_000":
            self.urgency_level = "emergency_000"
            self.escalation_flag = True
            self.intent = "emergency_000"
        elif urgency == "same_day" and self.urgency_level != "emergency_000":
            self.urgency_level = "same_day"
            if self.intent is None:
                self.intent = "same_day"

        lowered = text.lower()
        if "figtree" in lowered or any(
            f" {suburb} " in f" {lowered} " for suburb in SUBURB_OFFERS
        ):
            for suburb, _branch_id in SUBURB_OFFERS.items():
                if suburb in lowered:
                    offered = offer_branch_for_suburb(suburb, self.branch)
                    if offered:
                        self.offered_branch = offered
                    break

        botish = (
            "are you a real person",
            "are you a bot",
            "are you ai",
            "are you an ai",
            "are you a computer",
            "are you human",
        )
        if any(phrase in lowered for phrase in botish):
            self.bot_ask_count += 1

        clinician = match_clinician(text)
        if clinician:
            self.preferred_clinician = clinician

    def may_book(self) -> bool:
        return self.urgency_level != "emergency_000" and not (
            self.escalation_flag and self.urgency_level == "emergency_000"
        )

    def prompt_block(self) -> str:
        """Compact state the model must not contradict. Flow is already decided."""
        mobile_asks = int(self.ask_counts.get("mobile", 0))
        stop_mobile = self.should_stop_asking("mobile")
        offered = (
            get_branch(self.offered_branch).trading_name
            if self.offered_branch
            else "none"
        )
        return (
            "CALL STATE (enforced in code — do not contradict):\n"
            f"{self.known_facts_block()}\n"
            f"- answering as: {GROUP_TRADING_NAME}\n"
            f"- clinic: {self.branch_name} (id {self.branch}). They rang this clinic's "
            "number, so start here; move them to another clinic if that suits them better.\n"
            f"- caller_name: {self.caller_name or 'unknown'}\n"
            f"- caller_mobile: {self.caller_mobile or 'unknown'}\n"
            f"- is_existing_patient: {self.is_existing_patient}\n"
            f"- intent: {self.intent or 'unknown'}\n"
            f"- appointment_type: {self.appointment_type or 'unknown'}\n"
            f"- today (Australia/Sydney): {self.today_spoken} "
            f"(ISO {self.today.isoformat()}). Tomorrow is {self.tomorrow_spoken} "
            f"(ISO {self.tomorrow.isoformat()}).\n"
            f"- proposed_slot: {self.proposed_slot or 'none'}\n"
            f"- last_offer_slots: {self._offer_summary()}\n"
            f"- preferred_clinician: {self.preferred_clinician or 'none'}\n"
            f"- confirmed_slot: {self.confirmed_slot or 'none'}\n"
            f"- last_book_ok: {bool((self.last_book_result or {}).get('ok'))}\n"
            f"- last_book_confirmed: {bool((self.last_book_result or {}).get('confirmed'))}\n"
            f"- last_book_reason: {(self.last_book_result or {}).get('reason') or 'none'}\n"
            f"- booking_locked: {self.may_confirm_booking()}\n"
            f"- booking_phase: {self.booking_flow.phase.value}\n"
            f"- session_slot_ids: {len(self.booking_flow.session_slot_ids)}\n"
            f"- grounding_violations: {self.grounding_violations}\n"
            f"- speakable_dentist: {self.speakable.dentist_display_name or 'none'}\n"
            f"- availability_status: {self.speakable.availability_status or 'none'}\n"
            f"- date_resolved: {self.speakable.date_resolved}\n"
            f"- confirm_allowed: {self.speakable.confirm_allowed}\n"
            f"{self.date_context.prompt_line()}\n"
            f"- urgency_level: {self.urgency_level}\n"
            f"- escalation_flag: {self.escalation_flag}\n"
            f"- turn_count: {self.turn_count}\n"
            f"- mobile_asks: {mobile_asks}/{MAX_ASKS}; "
            f"stop_asking_mobile={stop_mobile}; "
            f"offer_callback={stop_mobile and self.may_offer_callback()}\n"
            f"- offered_branch: {offered}\n"
            f"- bot_ask_count: {self.bot_ask_count} "
            "(deflect once, then be honest)\n"
            f"- kill_switch: {self.kill_switch}\n"
            f"- used_acks: {self.used_phrases.get('ack', [])}\n"
            f"- barge_in_resume: {self.last_barge_in_resume or 'none'}\n"
            "- If urgency_level is emergency_000: do not book. Tell them triple zero "
            "or Shellharbour / Wollongong Hospital emergency. Do not offer a callback.\n"
            "- If urgency_level is same_day: this is URGENT. Do not offer a callback. "
            "Find a slot or transfer.\n"
            "- If offered_branch is set: offer that clinic warmly. Never a menu.\n"
            "- If stop_asking_mobile: do not ask for the mobile again.\n"
            "- If caller_mobile is already known: never ask for it again.\n"
            "- If barge_in_resume is set: start with that phrase. Never restart the "
            "cut-off sentence.\n"
            "- If last_offer_slots is none: do not name any clock time or diary "
            "slot. Call check_availability first. Only speak date, time, and "
            "clinician from those slot objects.\n"
            "- If last_offer_slots is set: offer those times. Do not search again "
            "unless they ask for a different day or dentist.\n"
            "- Never say you're all set, confirmed, booked, or similar unless "
            "booking_locked is true AND booking_phase is CONFIRMED. If it failed "
            "(slot_gone / invalid_slot_id), say the time is not locked yet.\n"
            "- If preferred_clinician is set, pass it to check_availability.\n"
            "- Use resolve_date_phrase for any day that is not today/tomorrow. "
            "If it is ambiguous, ask the caller. Never do date maths yourself.\n"
            "- If they ask what time it is, answer from DATE CONTEXT / "
            "current_time_sydney. Do not guess the clock. "
            "Never offer a slot whose start is before the current Sydney clock.\n"
            "- Speak date, time, and dentist only from SpeakableFacts / last tool "
            "results. If availability_status is UNKNOWN, never say chockers.\n"
            "- Use the Sydney today/tomorrow/clock lines above. Do not guess "
            "the weekday or the time of day."
        )

    def _offer_summary(self) -> str:
        if not self.last_availability_slots:
            return "none"
        parts: list[str] = []
        for slot in self.last_availability_slots[:2]:
            bits = [
                str(slot.get("date") or ""),
                str(slot.get("time") or ""),
                str(slot.get("clinician") or ""),
            ]
            parts.append(" ".join(bit for bit in bits if bit).strip())
        return "; ".join(parts) or "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "branch_name": self.branch_name,
            "caller_name": self.caller_name,
            "caller_mobile": self.caller_mobile,
            "is_existing_patient": self.is_existing_patient,
            "intent": self.intent,
            "appointment_type": self.appointment_type,
            "proposed_slot": self.proposed_slot,
            "confirmed_slot": self.confirmed_slot,
            "preferred_clinician": self.preferred_clinician,
            "today": self.today.isoformat(),
            "now": self.now.isoformat() if self.now else None,
            "period": self.date_context.period,
            "booking_locked": self.may_confirm_booking(),
            "urgency_level": self.urgency_level,
            "escalation_flag": self.escalation_flag,
            "turn_count": self.turn_count,
            "booking_phase": self.booking_flow.phase.value,
            "grounding_violations": self.grounding_violations,
            "availability_status": self.speakable.availability_status,
            "ask_counts": dict(self.ask_counts),
            "offered_branch": self.offered_branch,
            "bot_ask_count": self.bot_ask_count,
            "kill_switch": self.kill_switch,
        }
