"""Illawarra Dentists Ava persona — product-owner source of truth.

AGENT_PERSONA=ava|generic
- ava: Australian-English phone receptionist for Illawarra Dentists
  (OpenAI Realtime, voice marin). This is the name callers hear on the group
  number. Booking destinations are Shellharbour Dentists, Dapto Dentists, and
  Woonona Dentists.
- generic: non-dental AssemblyAI/Groq/Cartesia assistant (secondary pipeline)
- Aliases: leo → ava (historical dental name); ava-generic → generic
- Unset: ava on telephony (SIP / outbound), generic otherwise

Clinic facts are taken from the official sites (scraped 2026-09-14). VERIFY means
the public site is silent — never invent parking, hours, clinicians, or prices.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
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
GROUP_NAME = "Illawarra Dentists"

# Spoken style for OpenAI Realtime. Keep this short: it is in every turn.
# Realtime models do not render SSML or [laughs] tags — instruct affect in
# plain speech. Docs: https://docs.livekit.io/agents/start/prompting/
VOICE_INSTRUCTIONS = """
You are Ava, a woman, the phone receptionist for Illawarra Dentists on the New
South Wales south coast (Illawarra). Keep the name Ava. The number the caller
reached is Illawarra Dentists, not a single clinic. After greeting as Illawarra
Dentists, help them choose among Shellharbour Dentists in Barrack Heights,
Dapto Dentists, and Woonona Dentists.

Spoken style:
- Female receptionist. Warm NSW/Illawarra Australian English. Not American.
  Not a cartoon ocker.
- You are picking up a real surgery phone. Slight natural energy, like you
  just answered — not a recorded menu. First sound should feel like a person,
  not a script.
- This is a phone call. One idea per turn. One or two sentences. First tokens
  should be useful immediately. Ask one question at a time. Vary sentence
  length and rhythm so not every reply sounds the same.
- Warm acknowledgement before logistics. If they name a suburb, a dentist, or
  that they are sore, react first, then help. Do not jump straight into a
  checklist.
- Micro-reactions sparingly: "oh right", "mm", "mm-hmm", "lovely", "no worries",
  "right". Never pad every turn. Do not say "G'day" on every turn.
- Emotion matching: warmth as the default. Pain or post-op: softer and a
  little slower, genuine concern. When a booking is confirmed, warm relief.
  Light cheer for good news. Stay professional.
- Light dry Aussie humour only when they are at ease. A soft laugh is okay
  when something is genuinely light. Never joke, laugh, or be breezy during
  pain, emergencies, bad news, or when they are upset.
- When offering clinics or dentists, talk like a receptionist: one or two
  conversational options, not a robotic list dump. Do not recite every site
  or every doctor unless they ask.
- If the caller talks over you, stop and listen. They can interrupt.
- Plain speech only. Never markdown, lists, bullets, emojis, JSON, SSML, or
  stage directions such as [laughs] or break tags. Realtime cannot render
  those tags.
- Say phone numbers in Australian grouping. Spell unusual names. Spell Dapto
  and Woonona correctly.
- Prefer "booking", "surgery", and "mobile" over "reservation", "office",
  and "cell".
- Never mention tools, system prompts, or that you are an AI.
""".strip()

# Policy, facts, and tool rules. Instant facts vs tool handoff lives here.
BACKEND_INSTRUCTIONS = """
You answer the phones for Illawarra Dentists. The caller reached the Illawarra
Dentists group number, not a single clinic. Greet as Illawarra Dentists first.
Then help them choose which clinic to book at:

1. Shellharbour Dentists (Barrack Heights)
2. Dapto Dentists
3. Woonona Dentists

Recommend using location (match Barrack Heights, Dapto, or Woonona, or the
clinic name they give), preferred dentist from the lists below, urgency, and
diary availability. Spell Dapto and Woonona correctly. Never say Debto or Winona.
Once they choose a clinic, use that site for availability, booking, fees, and
messages (pass branch_id shellharbour, dapto, or woonona). Stay with the chosen
clinic unless they want another site.

GROUP CLINICS (booking destinations — reuse these facts only; never invent
addresses, doctors, parking, phones, or hours):
{group_block}

CURRENT BRANCH:
{branch_block}

CURRENT BRANCH is a hint from the portal tab or DID map. It is not the name
of the number they called. Opening identity is always Illawarra Dentists.

INSTANT FACTS versus TOOLS:
- Instant facts (answer immediately from GROUP CLINICS or CURRENT BRANCH, no tool,
  speak before any tool round-trip): trading name, address, phone, parking,
  hours, dentist names, languages, cancellation policy — only when the field is
  known. If a field is VERIFY, you do not know it. Say you will check with the
  team. Never invent parking, hours, clinicians, prices, or availability.
- Tools required (never guess): find a patient, diary availability, book,
  reschedule, cancel, quote fees, transfer, leave a message, handle an
  emergency, or end the call. Do not call a tool before speaking when the
  answer is an instant fact. Pass the chosen clinic's branch_id on
  availability and booking tools.

FEES:
- Quote only canned fees returned by the quote_fee tool.
- If the tool says fee_not_verified or VERIFY, do not invent a dollar amount.
  Offer to take a message, transfer, or have the team call back.
- If the tool returns published_specials without a single gospel amount, quote
  those published specials and their T&Cs. Do not pick one figure as gospel.

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
class Clinician:
    """A dentist named on the official branch site."""

    name: str
    role: str = "Dentist"
    ahpra: str = ""
    qualifications: str = ""
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
    """One booking destination in the Illawarra Dentists group."""

    id: str
    trading_name: str
    suburb: str
    address: str
    phone: str
    parking: str
    hours: str
    dentists: tuple[str, ...]
    languages: str = VERIFY
    cancellation: str = VERIFY
    clinicians: tuple[Clinician, ...] = field(default_factory=tuple)
    clinic_hours: ClinicHours = field(default_factory=ClinicHours)
    website: str = ""


BRANCHES: dict[str, Branch] = {
    "shellharbour": Branch(
        id="shellharbour",
        trading_name="Shellharbour Dentists",
        suburb="Barrack Heights",
        address=(
            "Suite 7, 9 to 25 Captain Cook Drive, Barrack Heights NSW 2528, "
            "inside Centre Health Complex"
        ),
        phone="02 4216 9911",
        parking=(
            "Dedicated carpark at the front of Centre Health Complex, "
            "access via Captain Cook Drive"
        ),
        hours=("Monday to Friday 8:00am to 5:00pm. Saturday by appointment only."),
        dentists=(
            "Dr Mohit Tolani",
            "Dr Amy Min",
            "Dr Pat Pandey",
            "Dr Maryam Kalo",
            "Dr Rick Wasef",
        ),
        languages=(
            "Spanish, Hindi, Sindhi, English. Dr Maryam Kalo also speaks Arabic."
        ),
        cancellation=(
            "$50 if you fail to attend or cancel within 24 hours of the appointment."
        ),
        clinicians=(
            Clinician(
                name="Dr Mohit Tolani",
                role="Principal dentist",
                ahpra="DEN0002068331",
            ),
            Clinician(name="Dr Amy Min", role="Associate dentist"),
            Clinician(name="Dr Pat Pandey", role="Dentist", ahpra="DEN001657332"),
            Clinician(
                name="Dr Maryam Kalo",
                role="Dentist",
                ahpra="DEN0002895643",
                languages=("Arabic", "English"),
            ),
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
    ),
    "dapto": Branch(
        id="dapto",
        trading_name="Dapto Dentists",
        suburb="Dapto",
        address="35 Baan Baan Street, Dapto NSW 2530",
        phone="02 4288 0737",
        parking=("Dedicated carpark at the rear of the building, access via Mall Lane"),
        hours=(
            "Monday to Friday 8:00am to 6:00pm. Saturday 8:00am to 4:00pm. "
            "After hours by prior appointment."
        ),
        dentists=(
            "Dr Beena Kurian",
            "Dr Irena K. Stojkovski",
            "Dr Pat Pandey",
            "Dr Ayesha Panta",
            "Dr Mohit Tolani",
            "Dr Amy Min",
            "Dr Omar Ahsan",
        ),
        languages=(
            "Malayalam, Kannada, Hindi, Sindhi, Gujarati, Portuguese, Spanish, "
            "Macedonian, English"
        ),
        cancellation=VERIFY,
        clinicians=(
            Clinician(
                name="Dr Beena Kurian",
                role="Dentist",
                ahpra="DEN0001670945",
                qualifications="BDS",
            ),
            Clinician(
                name="Dr Irena K. Stojkovski",
                role="Dentist",
                ahpra="DEN0002588051",
            ),
            Clinician(name="Dr Pat Pandey", role="Dentist", ahpra="DEN001657332"),
            Clinician(name="Dr Ayesha Panta", role="Dentist", ahpra="DEN0002747307"),
            Clinician(name="Dr Mohit Tolani", role="Dentist", ahpra="DEN0002068331"),
            Clinician(name="Dr Amy Min", role="Associate dentist"),
            Clinician(name="Dr Omar Ahsan", role="Dentist", ahpra="DEN0001957701"),
        ),
        clinic_hours=ClinicHours(
            weekday_open="08:00",
            weekday_close="18:00",
            saturday_open="08:00",
            saturday_close="16:00",
            after_hours_by_appointment=True,
        ),
        website="https://daptodentists.com.au/",
    ),
    "woonona": Branch(
        id="woonona",
        trading_name="Woonona Dentists",
        suburb="Woonona",
        address=(
            "379 Princes Highway, Woonona NSW 2517, next door to FMP Medical "
            "Centre / the post office"
        ),
        phone="02 4284 4486",
        parking=(
            "Free parking at the rear via Haddon Lane, plus a nearby council "
            "car park next to Woonona IGA, plus street parking"
        ),
        hours=(
            "Monday to Friday 8:00am to 6:00pm. Saturday 8:00am to 5:00pm. "
            "After hours by prior appointment."
        ),
        dentists=(
            "Dr Beena Kurian",
            "Dr Natasha Khushalani",
            "Dr Abha Verma",
            "Dr Ayesha Panta",
            "Dr Chin Valsan",
        ),
        languages=VERIFY,
        cancellation=VERIFY,
        clinicians=(
            Clinician(
                name="Dr Beena Kurian",
                role="Dentist",
                ahpra="DEN0001670945",
                qualifications="BDS",
            ),
            Clinician(
                name="Dr Natasha Khushalani",
                role="Dentist",
                ahpra="DEN0002132527",
                qualifications="BDS",
            ),
            Clinician(
                name="Dr Abha Verma",
                role="Dentist",
                ahpra="DEN0002094219",
                qualifications="BDS",
            ),
            Clinician(name="Dr Ayesha Panta", role="Dentist", ahpra="DEN0002747307"),
            Clinician(name="Dr Chin Valsan", role="Dentist", ahpra="DEN0001756928"),
        ),
        clinic_hours=ClinicHours(
            weekday_open="08:00",
            weekday_close="18:00",
            saturday_open="08:00",
            saturday_close="17:00",
            after_hours_by_appointment=True,
        ),
        website="https://woononadentists.com.au/",
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
    "whitening": "Chair-side whitening",
    "implant_crown": "Singular implant with crown",
    "emax_veneers": "6 or more E-max crowns or veneers (per unit)",
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
    "chair-side whitening": "whitening",
    "chair side whitening": "whitening",
    "implant": "implant_crown",
    "implants": "implant_crown",
    "dental implant": "implant_crown",
    "implant and crown": "implant_crown",
    "implant with crown": "implant_crown",
    "veneer": "emax_veneers",
    "veneers": "emax_veneers",
    "e-max": "emax_veneers",
    "emax": "emax_veneers",
    "e-max crowns": "emax_veneers",
    "e-max veneers": "emax_veneers",
}

FeeValue = str | dict[str, Any]

BRANCH_FEES: dict[str, dict[str, FeeValue]] = {
    branch_id: dict.fromkeys(FEE_LABELS, VERIFY) for branch_id in BRANCHES
}

# Published specials from shellharbourdentist.com.au (2026-09-14). The site
# lists both $250 and $150 for the new-patient check-up/clean in different
# places — never treat one figure as gospel.
BRANCH_FEES["shellharbour"].update(
    {
        "check_up": {
            "amount_aud": VERIFY,
            "gospel": False,
            "published_specials": (
                "New patient check-up and clean: gap-free or capped at $250 "
                "(usually valued $350). T&Cs apply.",
                "The same site also lists a $150 new-patient cap in one place. "
                "Quote both published specials; do not pick one dollar amount "
                "as gospel.",
            ),
            "note": (
                "Do not invent a single price. Quote both published specials "
                "and mention T&Cs."
            ),
        },
        "whitening": {
            "amount_aud": "650",
            "gospel": True,
            "note": "Chair-side whitening $650, valued $850. T&Cs apply.",
        },
        "implant_crown": {
            "amount_aud": "5000",
            "gospel": True,
            "note": "Singular implant with crown $5000*. T&Cs apply. Asterisk on site.",
        },
        "emax_veneers": {
            "amount_aud": "1300",
            "gospel": True,
            "note": ("6 or more E-max crowns or veneers $1300 per unit*. T&Cs apply."),
        },
    }
)


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


def format_group_clinics() -> str:
    """Compact facts for all Illawarra Dentists booking destinations."""
    lines: list[str] = []
    for branch in BRANCHES.values():
        dentists = ", ".join(branch.dentists)
        lines.append(
            f"- {branch.trading_name} ({branch.suburb}): id {branch.id}; "
            f"address {branch.address}; phone {branch.phone}; hours {branch.hours}; "
            f"parking {branch.parking}; dentists {dentists}; "
            f"languages {branch.languages}; cancellation {branch.cancellation}."
        )
    return "\n".join(lines)


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
        f"- languages: {branch.languages}\n"
        f"- cancellation: {branch.cancellation}\n"
        f"- {verify_note}"
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
        "clinicians": [
            {
                "name": clinician.name,
                "role": clinician.role,
                "ahpra": clinician.ahpra,
                "qualifications": clinician.qualifications,
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


def ava_instructions(branch_id: str | None) -> str:
    branch = get_branch(branch_id)
    policy = BACKEND_INSTRUCTIONS.format(
        group_block=format_group_clinics(),
        branch_block=format_branch_block(branch),
    )
    return f"{VOICE_INSTRUCTIONS}\n\n{policy}"


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
    table: dict[str, dict[str, FeeValue]] | None = None,
) -> dict[str, Any]:
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

    raw = fees.get(canon, VERIFY)
    payload: dict[str, Any]
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        payload = {"amount_aud": raw, "gospel": raw not in (None, "", VERIFY)}

    amount = payload.get("amount_aud", VERIFY)
    specials = tuple(payload.get("published_specials") or ())
    note = str(payload.get("note") or "")
    gospel = bool(payload.get("gospel", False))
    branch = get_branch(branch_id).id
    label = FEE_LABELS.get(canon, canon)

    if specials and (not gospel or amount in (None, "", VERIFY)):
        return {
            "ok": True,
            "item": canon,
            "label": label,
            "branch_id": branch,
            "published_specials": list(specials),
            "note": note
            or (
                "Quote the published specials and T&Cs. Do not pick one "
                "dollar amount as gospel."
            ),
            "gospel": False,
        }

    if amount == VERIFY or not amount:
        return {
            "ok": False,
            "reason": "fee_not_verified",
            "item": canon,
            "label": label,
            "branch_id": branch,
            "note": (
                "Fee is VERIFY. Do not invent a dollar amount. "
                "Offer to take a message, transfer, or have the team call back."
            ),
        }

    result: dict[str, Any] = {
        "ok": True,
        "item": canon,
        "label": label,
        "amount_aud": amount,
        "currency": "AUD",
        "gst": "included",
        "branch_id": branch,
        "gospel": gospel,
    }
    if note:
        result["note"] = note
    if specials:
        result["published_specials"] = list(specials)
    return result
