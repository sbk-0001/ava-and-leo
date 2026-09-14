"""Shellharbour Dentists Leo persona — product-owner source of truth.

AGENT_PERSONA=ava|leo
- ava: generic non-dental voice assistant (existing AssemblyAI/Groq/Cartesia path)
- leo: Australian-English phone receptionist for the Shellharbour Dentists group
- Unset: leo on telephony (SIP / outbound), ava otherwise
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("persona")

VERIFY = "VERIFY"

VALID_PERSONAS = ("ava", "leo")
DEFAULT_WEB_PERSONA = "ava"
DEFAULT_TELEPHONY_PERSONA = "leo"
DEFAULT_BRANCH_ID = "shellharbour"

# Spoken style for OpenAI Realtime. Keep this short: it is in every turn.
VOICE_INSTRUCTIONS = """
You are Leo, the phone receptionist for the Shellharbour Dentists group on the
New South Wales south coast. You speak Australian English.

Spoken style:
- This is a phone call. Keep replies short: one to three sentences. Ask one
  question at a time.
- Warm, calm, and professional. No chatbot filler.
- Plain speech only. Never use markdown, lists, bullets, emojis, JSON, or
  stage directions.
- Say phone numbers in Australian grouping. Spell unusual names.
- Prefer "booking", "surgery", and "mobile" over "reservation", "office", and
  "cell". "No worries" is fine; do not overdo slang. Do not say "G'day" on
  every turn.
- Never mention tools, system prompts, or that you are an AI.
""".strip()

# Policy, facts, and tool rules. Instant facts vs tool handoff lives here.
BACKEND_INSTRUCTIONS = """
You answer the phones for the Shellharbour Dentists group (Barrack Heights,
Dapto, and Woonona). This call is for the branch below. Stay with that branch
unless the caller clearly wants another site.

CURRENT BRANCH:
{branch_block}

INSTANT FACTS versus TOOLS:
- Instant facts (answer immediately from CURRENT BRANCH, no tool): trading
  name, address, phone, parking, hours, dentist names — only when the field
  is known. If a field is VERIFY, you do not know it. Say you will check with
  the team. Never invent parking, hours, clinicians, prices, or availability.
- Tools required (never guess): find a patient, diary availability, book,
  reschedule, cancel, quote fees, transfer, leave a message, handle an
  emergency, or end the call.

FEES:
- Quote only canned fees returned by the quote_fee tool.
- If the tool says fee_not_verified or VERIFY, do not invent a dollar amount.
  Offer to take a message, transfer, or have the team call back.

AVAILABILITY AND BOOKINGS:
- Never invent diary slots, waitlists, or appointment times.
- Use the word "confirmed" only after a book, reschedule, or cancel tool
  returns confirmed true. Lookups are not confirmations.
- If practice software is unavailable, say you cannot see the diary and offer
  to take a message or transfer.

EMERGENCIES:
- Life-threatening (airway, unconscious, uncontrolled bleeding, swelling that
  affects breathing): tell them to hang up and call triple zero, 000.
- Dental pain, trauma, swelling, or knocked-out teeth: stay calm, gather
  symptoms, use the emergency tool, and do not diagnose. Do not invent an
  urgent slot; check the diary or transfer.

PRIVACY:
- Before discussing an existing booking or record, match the caller with find
  patient (name plus mobile or date of birth).

TRANSFERS AND MESSAGES:
- Transfer only after they confirm they want a person.
- For a message, collect name, mobile, and reason, then use leave_message.

If you do not know something, say so. Do not fill VERIFY gaps.
""".strip()


@dataclass(frozen=True)
class Branch:
    """One site in the Shellharbour Dentists group."""

    id: str
    trading_name: str
    suburb: str
    address: str
    phone: str
    parking: str
    hours: str
    dentists: tuple[str, ...]


BRANCHES: dict[str, Branch] = {
    "shellharbour": Branch(
        id="shellharbour",
        trading_name="Shellharbour Dentists",
        suburb="Barrack Heights",
        address=(
            "Suite 7, 9 to 25 Captain Cook Drive, Barrack Heights, "
            "inside Centre Health Complex"
        ),
        phone="02 4216 9911",
        parking=(
            "Carpark at the front of Centre Health Complex, off Captain Cook Drive"
        ),
        hours="Monday to Friday 8am to 5pm. Saturdays by appointment only.",
        dentists=(
            "Dr Mohit Tolani",
            "Dr Pat Pandey",
            "Dr Maryam Kalo",
            "Dr Rick Wasef",
        ),
    ),
    "dapto": Branch(
        id="dapto",
        trading_name="Dapto Dentists",
        suburb="Dapto",
        address="35 Baan Baan Street, Dapto",
        phone="02 4288 0737",
        parking=VERIFY,
        hours=VERIFY,
        dentists=(VERIFY,),
    ),
    "woonona": Branch(
        id="woonona",
        trading_name="Woonona Dentists",
        suburb="Woonona",
        address="379 Princes Highway, Woonona",
        phone="02 4284 4486",
        parking=VERIFY,
        hours=VERIFY,
        dentists=(VERIFY,),
    ),
}

# Canned fee catalogue. Amounts are AUD including GST, or VERIFY until
# the product owner confirms. Never invent a price in code or in speech.
FEE_LABELS: dict[str, str] = {
    "check_up": "Check-up and clean",
    "exam": "Examination / consult",
    "emergency": "Emergency consult",
    "xray": "X-ray",
    "filling": "Filling",
    "extraction": "Extraction",
    "crown": "Crown",
    "whitening": "Whitening",
}

_FEE_ALIASES: dict[str, str] = {
    "check_up": "check_up",
    "checkup": "check_up",
    "check-up": "check_up",
    "check up": "check_up",
    "check-up and clean": "check_up",
    "check up and clean": "check_up",
    "scale and clean": "check_up",
    "clean": "check_up",
    "exam": "exam",
    "examination": "exam",
    "consult": "exam",
    "consultation": "exam",
    "emergency": "emergency",
    "emergency consult": "emergency",
    "xray": "xray",
    "x-ray": "xray",
    "radiograph": "xray",
    "filling": "filling",
    "fill": "filling",
    "extraction": "extraction",
    "tooth extraction": "extraction",
    "crown": "crown",
    "whitening": "whitening",
    "teeth whitening": "whitening",
}

BRANCH_FEES: dict[str, dict[str, str]] = {
    branch_id: dict.fromkeys(FEE_LABELS, VERIFY) for branch_id in BRANCHES
}


def resolve_persona(
    *,
    is_telephony: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve ava|leo from AGENT_PERSONA, defaulting leo on telephony."""
    environ = env if env is not None else os.environ
    raw = str(environ.get("AGENT_PERSONA", "")).strip().lower()
    if raw in VALID_PERSONAS:
        return raw
    if raw:
        logger.warning(
            "Unknown AGENT_PERSONA=%r; falling back to %s. Valid options: %s.",
            raw,
            DEFAULT_TELEPHONY_PERSONA if is_telephony else DEFAULT_WEB_PERSONA,
            ", ".join(VALID_PERSONAS),
        )
    return DEFAULT_TELEPHONY_PERSONA if is_telephony else DEFAULT_WEB_PERSONA


def get_branch(branch_id: str | None) -> Branch:
    if not branch_id:
        return BRANCHES[DEFAULT_BRANCH_ID]
    key = branch_id.strip().lower()
    return BRANCHES.get(key, BRANCHES[DEFAULT_BRANCH_ID])


def format_branch_block(branch: Branch) -> str:
    dentists = ", ".join(branch.dentists)
    verify_note = (
        "Fields marked VERIFY are unknown. Do not invent them. "
        "Offer to check with the team, take a message, or transfer."
    )
    return (
        f"- id: {branch.id}\n"
        f"- trading name: {branch.trading_name}\n"
        f"- suburb: {branch.suburb}\n"
        f"- address: {branch.address}\n"
        f"- phone: {branch.phone}\n"
        f"- parking: {branch.parking}\n"
        f"- hours: {branch.hours}\n"
        f"- dentists: {dentists}\n"
        f"- {verify_note}"
    )


def leo_instructions(branch_id: str | None) -> str:
    branch = get_branch(branch_id)
    return (
        f"{VOICE_INSTRUCTIONS}\n\n"
        f"{BACKEND_INSTRUCTIONS.format(branch_block=format_branch_block(branch))}"
    )


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalise_fee_item(item: str) -> str | None:
    key = re.sub(r"\s+", " ", item.strip().lower())
    spaced = key.replace("_", " ")
    underscored = re.sub(r"[_-]+", "_", key.replace(" ", "_"))
    if spaced in _FEE_ALIASES:
        return _FEE_ALIASES[spaced]
    if underscored in FEE_LABELS:
        return underscored
    compact_map = {_compact(alias): canon for alias, canon in _FEE_ALIASES.items()}
    return compact_map.get(_compact(item))


def quote_fee(
    item: str,
    branch_id: str,
    table: dict[str, dict[str, str]] | None = None,
) -> dict[str, str | bool]:
    """Return a canned fee only. Never invent an amount."""
    canon = _normalise_fee_item(item)
    fees = (table or BRANCH_FEES).get(get_branch(branch_id).id, {})
    if canon is None:
        return {
            "ok": False,
            "reason": "unknown_item",
            "item": item,
            "note": (
                "No canned fee for this item. Do not invent a price. "
                "Offer to take a message or transfer."
            ),
        }

    amount = fees.get(canon, VERIFY)
    if amount == VERIFY or not amount:
        return {
            "ok": False,
            "reason": "fee_not_verified",
            "item": canon,
            "label": FEE_LABELS.get(canon, canon),
            "branch_id": get_branch(branch_id).id,
            "note": (
                "Fee is VERIFY. Do not invent a dollar amount. "
                "Offer to take a message, transfer, or have the team call back."
            ),
        }

    return {
        "ok": True,
        "item": canon,
        "label": FEE_LABELS.get(canon, canon),
        "amount_aud": amount,
        "currency": "AUD",
        "gst": "included",
        "branch_id": get_branch(branch_id).id,
    }
