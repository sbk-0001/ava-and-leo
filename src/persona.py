"""Clinic facts, fees, persona switching, and session-instruction loader.

Spoken identity is the verbatim file in instructions/ava_receptionist.md
(interpolated with {{BRANCH_NAME}}). Clinic facts below are the product brief
only — invent nothing. VERIFY means unknown; Ava must not guess.

AGENT_PERSONA=ava|generic
- ava: Australian-English phone receptionist (OpenAI Realtime, voice marin)
- generic: non-dental AssemblyAI/Groq/Cartesia assistant
- Aliases: leo → ava; ava-generic → generic
- Unset: ava on telephony (SIP / outbound), generic otherwise
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("persona")

VERIFY = "VERIFY"

CANONICAL_PERSONAS = ("ava", "generic")
PERSONA_ALIASES = {
    "leo": "ava",
    "ava-generic": "generic",
}
VALID_PERSONAS = CANONICAL_PERSONAS + tuple(PERSONA_ALIASES)
DEFAULT_WEB_PERSONA = "generic"
DEFAULT_TELEPHONY_PERSONA = "ava"
DEFAULT_BRANCH_ID = "shellharbour"

INSTRUCTIONS_PATH = Path(__file__).parent / "instructions" / "ava_receptionist.md"
# The parent company. Every number answers as this; the three sites sit under it.
GROUP_TRADING_NAME = "Illawarra Dentists"

GROUP_NOTE = (
    "Same group, close to sixty years in the Illawarra between Woonona and Dapto."
)

# Kept for tests that still inspect spoken-style keywords. The live session
# loads the verbatim file via load_session_instructions — do not paraphrase it.
VOICE_INSTRUCTIONS = INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Clinician:
    """A dentist named in the product brief."""

    name: str
    role: str = "Dentist"
    languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClinicHours:
    """Parseable hours used by the mock diary. Spoken form lives on Branch.hours."""

    weekday_open: str = "08:00"
    weekday_close: str = "17:00"
    saturday_open: str | None = None
    saturday_close: str | None = None
    saturday_by_appointment: bool = False
    after_hours_by_appointment: bool = False


@dataclass(frozen=True)
class Branch:
    """One site. Only brief-approved facts; VERIFY otherwise."""

    id: str
    trading_name: str
    suburb: str
    address: str
    phone: str
    parking: str = VERIFY
    hours: str = VERIFY
    dentists: tuple[str, ...] = ()
    languages: str = VERIFY
    cancellation: str = (
        "24 hours notice required. $50 fee for a no-show or a cancellation "
        "inside 24 hours. Ava must mention the $50 when cancelling inside the "
        "window — warmly, never sternly — and she must never waive it herself."
    )
    clinicians: tuple[Clinician, ...] = field(default_factory=tuple)
    clinic_hours: ClinicHours = field(default_factory=ClinicHours)
    website: str = ""
    notes: str = ""


BRANCHES: dict[str, Branch] = {
    "shellharbour": Branch(
        id="shellharbour",
        trading_name="Shellharbour Dentists",
        suburb="Barrack Heights",
        address=(
            "Suite 7, 9-25 Captain Cook Drive, Barrack Heights NSW 2528, "
            "inside the Centre Health Complex"
        ),
        phone="(02) 4216 9911",
        parking=(
            "Dedicated carpark at the front of the complex, "
            "access off Captain Cook Drive."
        ),
        hours="Mon to Fri 8:00am - 5:00pm. Saturday by appointment only.",
        dentists=(
            "Dr Mohit Tolani",
            "Dr Pat Pandey",
            "Dr Maryam Kalo",
            "Dr Rick Wasef",
        ),
        languages="English, Hindi, Sindhi, Spanish",
        clinicians=(
            Clinician(name="Dr Mohit Tolani", role="Principal dentist"),
            Clinician(name="Dr Pat Pandey", role="Dentist"),
            Clinician(name="Dr Maryam Kalo", role="Dentist"),
            Clinician(name="Dr Rick Wasef", role="Associate dentist"),
        ),
        clinic_hours=ClinicHours(
            weekday_open="08:00",
            weekday_close="17:00",
            saturday_open="09:00",
            saturday_close="11:00",
            saturday_by_appointment=True,
        ),
        website="https://shellharbourdentist.com.au/",
        notes=(
            "Established 2024, previously Centre Health Dental, "
            "serving the area 18 years. Primary site."
        ),
    ),
    "dapto": Branch(
        id="dapto",
        trading_name="Dapto Dentists",
        suburb="Dapto",
        address="35 Baan Baan Street, Dapto NSW 2530",
        phone="(02) 4288 0737",
        website="https://daptodentists.com.au/",
        # Diary seed hours only — not published in the brief, never spoken as fact.
        clinic_hours=ClinicHours(
            weekday_open="08:00",
            weekday_close="17:00",
        ),
        notes=GROUP_NOTE,
    ),
    "woonona": Branch(
        id="woonona",
        trading_name="Woonona Dentists",
        suburb="Woonona",
        address="379 Princes Highway, Woonona NSW 2517",
        phone="(02) 4284 4486",
        website="https://woononadentists.com.au/",
        clinic_hours=ClinicHours(
            weekday_open="08:00",
            weekday_close="17:00",
        ),
        notes=GROUP_NOTE,
    ),
}

FEE_LABELS: dict[str, str] = {
    "check_up": "New patient check-up and clean",
    "whitening": "Chair-side teeth whitening",
    "implant_consult": "Free dental implant consult",
    "smile_consult": "Free smile makeover and cosmetic consult",
    "wisdom_consult": "Free wisdom teeth removal consult including OPG x-ray",
    "implant_crown": "Single implant with crown",
    "emax_veneers": "E-max crowns or veneers, six or more (per unit)",
    "wisdom_removal": "Wisdom tooth removal (per tooth)",
    "child_cdbs": "Children 2 to 17 — Child Dental Benefits Schedule",
    "health_funds": "Health funds and HICAPS",
    "payment": "Payment options",
    "ohffss": "NSW Oral Health voucher (OHFFSS)",
}

_FEE_ALIASES: dict[str, str] = {
    "check_up": "check_up",
    "checkup": "check_up",
    "check-up": "check_up",
    "check up": "check_up",
    "check-up and clean": "check_up",
    "check up and clean": "check_up",
    "scale and clean": "check_up",
    "new patient": "check_up",
    "new patient special": "check_up",
    "clean": "check_up",
    "whitening": "whitening",
    "teeth whitening": "whitening",
    "chair-side whitening": "whitening",
    "chair side whitening": "whitening",
    "implant consult": "implant_consult",
    "implant consultation": "implant_consult",
    "free implant consult": "implant_consult",
    "smile makeover": "smile_consult",
    "cosmetic consult": "smile_consult",
    "free consult": "implant_consult",
    "wisdom consult": "wisdom_consult",
    "wisdom teeth consult": "wisdom_consult",
    "implant": "implant_crown",
    "implants": "implant_crown",
    "dental implant": "implant_crown",
    "implant and crown": "implant_crown",
    "implant with crown": "implant_crown",
    "single implant": "implant_crown",
    "veneer": "emax_veneers",
    "veneers": "emax_veneers",
    "e-max": "emax_veneers",
    "emax": "emax_veneers",
    "e-max crowns": "emax_veneers",
    "e-max veneers": "emax_veneers",
    "wisdom": "wisdom_removal",
    "wisdom tooth": "wisdom_removal",
    "wisdom teeth": "wisdom_removal",
    "wisdom tooth removal": "wisdom_removal",
    "child dental": "child_cdbs",
    "cdbs": "child_cdbs",
    "bulk billed": "child_cdbs",
    "kids": "child_cdbs",
    "children": "child_cdbs",
    "hcf": "health_funds",
    "cbhs": "health_funds",
    "smile.com.au": "health_funds",
    "smile dot com": "health_funds",
    "hicaps": "health_funds",
    "health fund": "health_funds",
    "health funds": "health_funds",
    "preferred provider": "health_funds",
    "afterpay": "payment",
    "zip": "payment",
    "zip pay": "payment",
    "supercare": "payment",
    "super": "payment",
    "payment": "payment",
    "ohffss": "ohffss",
    "oral health voucher": "ohffss",
    "nsw oral health": "ohffss",
    "voucher": "ohffss",
}

# Group-wide fee table from the product brief. Quote only these.
FEE_TABLE: dict[str, dict[str, Any]] = {
    "check_up": {
        "status": "known",
        "speak": (
            "New patient check-up and clean: gap free for health fund holders, "
            "or capped at $250 if no cover. Normally valued at $350. Includes "
            "comprehensive exam, scale and clean, up to two digital x-rays, "
            "fluoride, and a printed treatment plan. Not combinable with other offers."
        ),
    },
    "whitening": {
        "status": "known",
        "amount_aud": "650",
        "speak": (
            "Chair-side teeth whitening is $650, normally $850. "
            "Afterpay or Zip in four instalments."
        ),
    },
    "implant_consult": {
        "status": "known",
        "speak": "Free consult for dental implants, valued at $450.",
    },
    "smile_consult": {
        "status": "known",
        "speak": "Free consult for smile makeover and cosmetic, valued at $350.",
    },
    "wisdom_consult": {
        "status": "known",
        "speak": ("Free consult for wisdom teeth removal including OPG x-ray."),
    },
    "implant_crown": {
        "status": "known",
        "amount_aud": "4500",
        "speak": "Single implant with crown from $4,500.",
    },
    "emax_veneers": {
        "status": "known",
        "amount_aud": "1300",
        "speak": "E-max crowns or veneers, six or more, $1,300 per unit.",
    },
    "wisdom_removal": {
        "status": "known",
        "speak": "Wisdom tooth removal is $350 to $500 per tooth.",
    },
    "child_cdbs": {
        "status": "known",
        "speak": (
            "Children 2 to 17: up to $1,000 bulk billed under the "
            "Child Dental Benefits Schedule."
        ),
    },
    "health_funds": {
        "status": "known",
        "speak": (
            "Preferred provider for HCF, CBHS and Smile.com.au. "
            "HICAPS on the spot for all funds."
        ),
    },
    "payment": {
        "status": "known",
        "speak": (
            "Payment: cash, card, HICAPS, Zip Pay, Afterpay, SuperCare, "
            "and in-house help applying to access super."
        ),
    },
    "ohffss": {
        "status": "known",
        "speak": (
            "NSW Oral Health voucher (OHFFSS) for emergency care — "
            "eligibility line is 1300 369 651."
        ),
    },
}

FeeValue = str | dict[str, Any]


def canonical_persona(raw: str) -> str | None:
    """Map AGENT_PERSONA / job metadata to ava|generic. Aliases: leo, ava-generic."""
    key = raw.strip().lower()
    if key in PERSONA_ALIASES:
        return PERSONA_ALIASES[key]
    if key in CANONICAL_PERSONAS:
        return key
    return None


def resolve_persona(
    *,
    is_telephony: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve ava|generic from AGENT_PERSONA, defaulting ava on telephony."""
    environ = env if env is not None else os.environ
    raw = str(environ.get("AGENT_PERSONA", "")).strip().lower()
    resolved = canonical_persona(raw) if raw else None
    if resolved:
        return resolved
    if raw:
        logger.warning(
            "Unknown AGENT_PERSONA=%r; falling back to %s. Valid options: %s.",
            raw,
            DEFAULT_TELEPHONY_PERSONA if is_telephony else DEFAULT_WEB_PERSONA,
            ", ".join(CANONICAL_PERSONAS),
        )
    return DEFAULT_TELEPHONY_PERSONA if is_telephony else DEFAULT_WEB_PERSONA


def get_branch(branch_id: str | None) -> Branch:
    if not branch_id:
        return BRANCHES[DEFAULT_BRANCH_ID]
    key = branch_id.strip().lower()
    return BRANCHES.get(key, BRANCHES[DEFAULT_BRANCH_ID])


def resolve_tool_branch(requested: str | None, current: str | None) -> str:
    """Clinic id for availability/book tools. Unknown ids fall back like get_branch."""
    if requested and str(requested).strip():
        return get_branch(requested).id
    return get_branch(current).id


def format_branch_block(branch: Branch) -> str:
    dentists = ", ".join(branch.dentists) if branch.dentists else VERIFY
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
        f"- languages: {branch.languages}\n"
        f"- cancellation: {branch.cancellation}\n"
        f"- notes: {branch.notes or VERIFY}\n"
        f"- {verify_note}"
    )


def format_group_clinics() -> str:
    lines: list[str] = [GROUP_NOTE]
    for branch in BRANCHES.values():
        dentists = ", ".join(branch.dentists) if branch.dentists else VERIFY
        lines.append(
            f"- {branch.trading_name} ({branch.suburb}): id {branch.id}; "
            f"address {branch.address}; phone {branch.phone}; hours {branch.hours}; "
            f"parking {branch.parking}; dentists {dentists}; "
            f"languages {branch.languages}."
        )
    return "\n".join(lines)


def format_clinic_facts(branch_id: str | None) -> str:
    branch = get_branch(branch_id)
    return (
        "CLINIC FACTS (quote only these; never extrapolate):\n"
        f"{format_branch_block(branch)}\n\n"
        f"OTHER SITES IN THE GROUP:\n{format_group_clinics()}\n"
        "You book at the current branch by default. Offer another site only if "
        "the caller raises it or a suburb clearly suits one better."
    )


def branch_as_dict(branch: Branch) -> dict[str, Any]:
    return {
        "id": branch.id,
        "trading_name": branch.trading_name,
        "suburb": branch.suburb,
        "address": branch.address,
        "phone": branch.phone,
        "parking": branch.parking,
        "hours": branch.hours,
        "dentists": list(branch.dentists),
        "languages": branch.languages,
        "cancellation": branch.cancellation,
        "website": branch.website,
        "notes": branch.notes,
        "clinicians": [
            {
                "name": clinician.name,
                "role": clinician.role,
                "languages": list(clinician.languages),
            }
            for clinician in (branch.clinicians or ())
        ],
        "hours_spec": {
            "weekday_open": branch.clinic_hours.weekday_open,
            "weekday_close": branch.clinic_hours.weekday_close,
            "saturday_open": branch.clinic_hours.saturday_open,
            "saturday_close": branch.clinic_hours.saturday_close,
            "saturday_by_appointment": branch.clinic_hours.saturday_by_appointment,
            "after_hours_by_appointment": (
                branch.clinic_hours.after_hours_by_appointment
            ),
        },
    }


def load_session_instructions(branch_name: str) -> str:
    """Load the verbatim receptionist block and interpolate {{BRANCH_NAME}}."""
    text = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    if "{{BRANCH_NAME}}" not in text:
        raise RuntimeError("instructions file missing {{BRANCH_NAME}} placeholder")
    return text.replace("{{BRANCH_NAME}}", branch_name)


def ava_instructions(branch_id: str | None, state_block: str | None = None) -> str:
    branch = get_branch(branch_id)
    parts = [
        # She answers for the practice, not the site the DID maps to.
        load_session_instructions(GROUP_TRADING_NAME),
        format_clinic_facts(branch.id),
    ]
    if state_block:
        parts.append(state_block)
    return "\n\n".join(parts)


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
    branch_id: str | None = None,
    table: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a canned fee only. Unknown items are unknown — never guess."""
    del branch_id  # fees are group-wide
    canon = _normalise_fee_item(item)
    source = table if table is not None else FEE_TABLE
    if canon is None or canon not in source:
        return {
            "ok": False,
            "status": "unknown",
            "reason": "unknown_item",
            "item": item,
            "note": (
                "No fee in the table. Do not invent a price. "
                "Offer to take a message or have the team call back."
            ),
        }

    payload = dict(source[canon])
    label = FEE_LABELS.get(canon, canon)
    result: dict[str, Any] = {
        "ok": True,
        "status": "known",
        "item": canon,
        "label": label,
        "speak": payload.get("speak", ""),
    }
    if payload.get("amount_aud"):
        result["amount_aud"] = payload["amount_aud"]
        result["currency"] = "AUD"
    return result
