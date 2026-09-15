"""CallState is the single source of truth for Ava's flow.

Prompt-only counters have failed twice. Ask loops, branch identity, urgency and
escalation live here and are injected into the session before Ava speaks.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from persona import DEFAULT_BRANCH_ID, get_branch

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

_MOBILE_RE = re.compile(r"^(?:\+?61|0)4\d{8}$")

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
        if self.should_stop_asking("mobile"):
            return {
                "ok": False,
                "stop_asking": True,
                "offer_callback": True,
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
        return {
            "ok": False,
            "stop_asking": result.stop_asking,
            "offer_callback": result.offer_callback,
            "attempts": result.count,
            "remaining": max(0, MAX_ASKS - result.count),
            "note": (
                "Do not ask for the mobile again. Offer a callback."
                if result.stop_asking
                else "Ask once more for the mobile, one question only."
            ),
        }

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
            f"- branch: {self.branch_name} (id {self.branch}). They rang this number.\n"
            f"- caller_name: {self.caller_name or 'unknown'}\n"
            f"- caller_mobile: {self.caller_mobile or 'unknown'}\n"
            f"- is_existing_patient: {self.is_existing_patient}\n"
            f"- intent: {self.intent or 'unknown'}\n"
            f"- appointment_type: {self.appointment_type or 'unknown'}\n"
            f"- proposed_slot: {self.proposed_slot or 'none'}\n"
            f"- confirmed_slot: {self.confirmed_slot or 'none'}\n"
            f"- urgency_level: {self.urgency_level}\n"
            f"- escalation_flag: {self.escalation_flag}\n"
            f"- turn_count: {self.turn_count}\n"
            f"- mobile_asks: {mobile_asks}/{MAX_ASKS}; "
            f"stop_asking_mobile={stop_mobile}; offer_callback={stop_mobile}\n"
            f"- offered_branch: {offered}\n"
            f"- bot_ask_count: {self.bot_ask_count} "
            "(deflect once, then be honest)\n"
            f"- kill_switch: {self.kill_switch}\n"
            "- If urgency_level is emergency_000: do not book. Tell them triple zero "
            "or Shellharbour / Wollongong Hospital emergency.\n"
            "- If offered_branch is set: offer that clinic warmly. Never a menu.\n"
            "- If stop_asking_mobile: do not ask for the mobile again."
        )

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
            "urgency_level": self.urgency_level,
            "escalation_flag": self.escalation_flag,
            "turn_count": self.turn_count,
            "ask_counts": dict(self.ask_counts),
            "offered_branch": self.offered_branch,
            "bot_ask_count": self.bot_ask_count,
            "kill_switch": self.kill_switch,
        }
