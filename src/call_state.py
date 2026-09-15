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
    ingest_date_resolution,
)
from persona import BRANCHES, DEFAULT_BRANCH_ID, get_branch
from phrase_pools import ACKS, BARGE_IN_RESUME, CLOSINGS, OPENINGS, pick_from_pool

MAX_ASKS = 3

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
    preferred_branch: str | None = None
    usual_dentist: str | None = None
    last_appointment_private: dict[str, Any] | None = None
    pms_record: dict[str, Any] | None = None
    pending_grounding_note: str | None = None

    def __post_init__(self) -> None:
        live = datetime.now(SYDNEY)
        if self.now is not None:
            if self.now.tzinfo is None:
                self.now = self.now.replace(tzinfo=SYDNEY)
            else:
                self.now = self.now.astimezone(SYDNEY)
            self.today = self.now.date()
        elif self.today == live.date():
            self.now = live
        else:
            self.now = datetime.combine(self.today, datetime.min.time(), tzinfo=SYDNEY)
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
        elif today is not None:
            self.today = today
            clock = self.now or datetime.now(SYDNEY)
            self.now = datetime.combine(today, clock.timetz().replace(tzinfo=SYDNEY))
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
        return (
            "KNOWN FACTS (already collected — never ask again):\n"
            f"- first_name: {self.caller_first_name or 'unknown'}\n"
            f"- caller_name: {name}\n"
            f"- caller_mobile: {mobile}\n"
            f"- known_caller: {self.known_caller}\n"
            f"- ani: {self.ani or 'none'}\n"
            f"- dob_verified: {self.dob_verified}\n"
            f"- is_existing_patient: {patient_status}\n"
            f"- preferred_branch: {self.preferred_branch or self.branch}\n"
            f"- usual_dentist: {dentist}\n"
            f"- last_appointment: {last_appt}\n"
            "- If caller_mobile is set: do not ask for their number. "
            "You may light-confirm 'Is this still the best number for ya?'\n"
            "- New booking: no DOB required. Discuss/move/cancel existing: DOB required.\n"
            "- Do not volunteer existing appointment, dentist, or treatment detail "
            "until dob_verified is true.\n"
            "- Failed DOB: offer a callback. Do not mention date of birth. "
            "Do not confirm or deny a record."
        )

    @property
    def caller_first_name(self) -> str | None:
        if not self.caller_name:
            return None
        token = self.caller_name.strip().split()[0]
        return token or None

    def may_disclose_existing(self) -> bool:
        return self.dob_verified and not self.dob_failed

    def require_dob_for_existing(self) -> dict[str, Any]:
        if self.may_disclose_existing():
            return {"ok": True, "verified": True}
        if self.dob_failed:
            return {
                "ok": False,
                "reason": "verification_failed",
                "note": (
                    "Offer to have the team call back. Do not mention date of birth. "
                    "Do not confirm or deny a record."
                ),
            }
        return {
            "ok": False,
            "reason": "dob_required",
            "note": (
                "Need date of birth before discussing, moving, or cancelling an "
                "existing appointment. Ask once for DOB. Do not mention any "
                "existing booking details."
            ),
        }

    def verify_dob(self, given: str | None) -> dict[str, Any]:
        expected = ""
        record = self.pms_record or {}
        patients = (
            record.get("patients") if isinstance(record.get("patients"), list) else []
        )
        if patients and isinstance(patients[0], dict):
            expected = str(patients[0].get("date_of_birth") or "")
        given_n = re.sub(r"[^0-9]", "", given or "")
        expected_n = re.sub(r"[^0-9]", "", expected)
        if expected_n and given_n and given_n == expected_n:
            self.dob_verified = True
            self.dob_failed = False
            if record.get("is_existing_patient"):
                self.is_existing_patient = True
            return {
                "ok": True,
                "verified": True,
                "note": "Identity verified for this call.",
            }
        self.dob_failed = True
        self.dob_verified = False
        return {
            "ok": False,
            "reason": "verification_failed",
            "note": (
                "Offer to have the team call back. Do not mention date of birth. "
                "Do not confirm or deny a record."
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
        name = branch_name or self.branch_name
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
            "CALL STATE (enforced in code — do not contradict, do not ask which branch):\n"
            f"{self.known_facts_block()}\n"
            f"- branch: {self.branch_name} (id {self.branch}). They rang this number.\n"
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
