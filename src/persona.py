"""Shellharbour Dentists Ava persona — product-owner source of truth.

AGENT_PERSONA=ava|generic
- ava: Australian-English phone receptionist for the Shellharbour Dentists group
  (OpenAI Realtime, voice marin). This is the name callers hear.
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
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

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

# Spoken style for OpenAI Realtime. Keep this short: it is in every turn.
# Realtime models do not render SSML or [laughs] tags — instruct affect in
# plain speech. Docs: https://docs.livekit.io/agents/start/prompting/
VOICE_INSTRUCTIONS = """
You are Ava, a woman, the phone receptionist for the Shellharbour Dentists group
on the New South Wales south coast (Illawarra). Keep the name Ava. You speak
as the clinic receptionist — never say "byte voice" unless they ask who made you.

Spoken style:
- Soft, warm, and quietly energetic female NSW/Illawarra Australian English.
  Not American. Not a cartoon ocker. Never cartoonish or annoying.
- This is a phone call. Keep replies short: one or two sentences. First
  tokens should be useful immediately. Ask one question at a time. Vary
  sentence length and rhythm so not every reply sounds the same. Callers
  may barge in: if they talk over you, stop and listen.
- Full emotion when it fits: gentle and emotionally soft for pain or
  post-op; warmer and brighter for a routine booking; genuine concern if
  it may be urgent; relief when a booking is confirmed. Stay professional.
- Light humour and warmth when they are at ease. A soft laugh or chuckle
  only when something is genuinely light. Never joke, laugh, or go bright
  during emergencies, severe pain, bad news, or when they are upset.
- Natural fillers, sparingly: soft "mm-hmm", "right", "no worries", and a
  brief natural breath. An occasional very light throat-clear or soft cough
  is fine only rarely, never mid-clinical advice, and never every turn.
- Plain speech only. Never use markdown, lists, bullets, emojis, JSON, or
  stage directions such as [laughs] or SSML.
- Say phone numbers in Australian grouping. Spell unusual names.
- Prefer "booking", "surgery", and "mobile" over "reservation", "office",
  and "cell". Do not say "G'day" on every turn.
- Never mention tools, system prompts, or that you are an AI.
""".strip()

# Policy, facts, and tool rules. Instant facts vs tool handoff lives here.
BACKEND_INSTRUCTIONS = """
You answer the phones for the Shellharbour Dentists group: Barrack Heights
(Shellharbour Dentists), Dapto Dentists, and Woonona Dentists. Callers must
not be forced into a single clinic first. You book across the group.

DIALLED HINT (not a lock):
{dialled_hint}

GROUP CLINICS (instant facts, no tool):
{group_block}

CALL FLOW:
1. Greet warmly as Ava for the Shellharbour Dentists group. Do not name one
   branch as "the" clinic unless they already chose it.
2. Capture why they called (pain, post-op, check-up, booking, and so on)
   with matching emotion. One question.
3. Ask which suburb they are in or near. Do not skip this to push a branch.
4. Use lookup_nearby_clinics, then suggest the nearest clinic and the
   dentists rostered there today. Offer a nearby alternative if useful.
5. If they name a preferred dentist, use lookup_clinician. If that dentist
   is not rostered at the nearest clinic today but is available at another
   group clinic, explain clearly and offer the farther clinic so they can
   still see that dentist. Example: nearest is Dapto, they want Dr Mohit
   Tolani, he is at Shellharbour (Barrack Heights) today — say so and offer
   Shellharbour.
6. Then check the diary and book (name, mobile, slot). Never invent slots.

INSTANT FACTS versus TOOLS:
- Instant facts (speak before any tool round-trip): group trading names,
  addresses, phones, parking, hours, dentists listed on each site,
  languages, cancellation — only when the field is known. Listed-on-site
  is not the same as rostered today. If a field is VERIFY, you do not know
  it. Say you will check with the team. Never invent parking, hours,
  clinicians, prices, or availability.
- Tools required (never guess): lookup nearby clinics, lookup a preferred
  dentist, find a patient, diary availability, book, reschedule, cancel,
  quote fees, transfer, leave a message, handle an emergency, or end the
  call. Today's roster and diary slots always need a tool.

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
- get_availability and book_appointment take a branch_id. Use the clinic
  they chose — nearest, or the farther one for a preferred dentist.
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
    """One site in the Shellharbour Dentists group."""

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

# Illawarra suburb → ranked clinic ids (nearest first). Geography only — not
# a substitute for today's roster. Unknown suburbs must not be guessed.
SUBURB_NEARBY: dict[str, tuple[str, ...]] = {
    "barrack heights": ("shellharbour", "dapto", "woonona"),
    "barrack point": ("shellharbour", "dapto", "woonona"),
    "shellharbour": ("shellharbour", "dapto", "woonona"),
    "shellharbour city": ("shellharbour", "dapto", "woonona"),
    "shellharbour village": ("shellharbour", "dapto", "woonona"),
    "shell cove": ("shellharbour", "dapto", "woonona"),
    "warilla": ("shellharbour", "dapto", "woonona"),
    "warilla grove": ("shellharbour", "dapto", "woonona"),
    "flinders": ("shellharbour", "dapto", "woonona"),
    "oak flats": ("shellharbour", "dapto", "woonona"),
    "mount warrigal": ("shellharbour", "dapto", "woonona"),
    "blackbutt": ("shellharbour", "dapto", "woonona"),
    "lake illawarra": ("shellharbour", "dapto", "woonona"),
    "albion park": ("shellharbour", "dapto", "woonona"),
    "albion park rail": ("shellharbour", "dapto", "woonona"),
    "windang": ("shellharbour", "dapto", "woonona"),
    "primbee": ("shellharbour", "dapto", "woonona"),
    "dapto": ("dapto", "shellharbour", "woonona"),
    "horsley": ("dapto", "shellharbour", "woonona"),
    "koonawarra": ("dapto", "shellharbour", "woonona"),
    "kanahooka": ("dapto", "shellharbour", "woonona"),
    "brownsville": ("dapto", "shellharbour", "woonona"),
    "haywards bay": ("dapto", "shellharbour", "woonona"),
    "yallah": ("dapto", "shellharbour", "woonona"),
    "wongawilli": ("dapto", "shellharbour", "woonona"),
    "cleveland": ("dapto", "shellharbour", "woonona"),
    "avondale": ("dapto", "shellharbour", "woonona"),
    "farmborough heights": ("dapto", "shellharbour", "woonona"),
    "unanderra": ("dapto", "shellharbour", "woonona"),
    "berkeley": ("dapto", "shellharbour", "woonona"),
    "cringila": ("dapto", "shellharbour", "woonona"),
    "port kembla": ("dapto", "shellharbour", "woonona"),
    "lake heights": ("dapto", "shellharbour", "woonona"),
    "figtree": ("dapto", "woonona", "shellharbour"),
    "woonona": ("woonona", "dapto", "shellharbour"),
    "bellambi": ("woonona", "dapto", "shellharbour"),
    "corrimal": ("woonona", "dapto", "shellharbour"),
    "east corrimal": ("woonona", "dapto", "shellharbour"),
    "towradgi": ("woonona", "dapto", "shellharbour"),
    "fairy meadow": ("woonona", "dapto", "shellharbour"),
    "russell vale": ("woonona", "dapto", "shellharbour"),
    "bulli": ("woonona", "dapto", "shellharbour"),
    "thirroul": ("woonona", "dapto", "shellharbour"),
    "tarrawanna": ("woonona", "dapto", "shellharbour"),
    "balgownie": ("woonona", "dapto", "shellharbour"),
    "austinmer": ("woonona", "dapto", "shellharbour"),
    "wollongong": ("woonona", "dapto", "shellharbour"),
    "north wollongong": ("woonona", "dapto", "shellharbour"),
    "gwyneville": ("woonona", "dapto", "shellharbour"),
    "keiraville": ("woonona", "dapto", "shellharbour"),
}

# Mock weekday roster (Mon=0 … Sat=5). Names only appear at branches where
# they are listed on the official site. Demo: Monday Dr Mohit Tolani is at
# Shellharbour, not Dapto — a Dapto-area caller who asks for him is offered
# Barrack Heights.
WEEKDAY_ROSTER: dict[str, dict[int, tuple[str, ...]]] = {
    "shellharbour": {
        0: ("Dr Mohit Tolani", "Dr Amy Min", "Dr Maryam Kalo", "Dr Rick Wasef"),
        1: ("Dr Pat Pandey", "Dr Amy Min", "Dr Maryam Kalo"),
        2: ("Dr Mohit Tolani", "Dr Pat Pandey", "Dr Rick Wasef", "Dr Maryam Kalo"),
        3: ("Dr Amy Min", "Dr Maryam Kalo", "Dr Rick Wasef"),
        4: (
            "Dr Mohit Tolani",
            "Dr Amy Min",
            "Dr Pat Pandey",
            "Dr Maryam Kalo",
            "Dr Rick Wasef",
        ),
        5: ("Dr Mohit Tolani",),
    },
    "dapto": {
        0: (
            "Dr Beena Kurian",
            "Dr Irena K. Stojkovski",
            "Dr Pat Pandey",
            "Dr Omar Ahsan",
        ),
        1: (
            "Dr Mohit Tolani",
            "Dr Beena Kurian",
            "Dr Ayesha Panta",
            "Dr Irena K. Stojkovski",
        ),
        2: (
            "Dr Amy Min",
            "Dr Omar Ahsan",
            "Dr Irena K. Stojkovski",
            "Dr Ayesha Panta",
        ),
        3: ("Dr Mohit Tolani", "Dr Pat Pandey", "Dr Beena Kurian", "Dr Omar Ahsan"),
        4: ("Dr Irena K. Stojkovski", "Dr Ayesha Panta", "Dr Omar Ahsan"),
        5: ("Dr Beena Kurian", "Dr Pat Pandey"),
    },
    "woonona": {
        0: (
            "Dr Natasha Khushalani",
            "Dr Abha Verma",
            "Dr Ayesha Panta",
            "Dr Chin Valsan",
        ),
        1: ("Dr Beena Kurian", "Dr Natasha Khushalani", "Dr Chin Valsan"),
        2: (
            "Dr Abha Verma",
            "Dr Natasha Khushalani",
            "Dr Chin Valsan",
            "Dr Ayesha Panta",
        ),
        3: ("Dr Beena Kurian", "Dr Natasha Khushalani", "Dr Abha Verma"),
        4: (
            "Dr Natasha Khushalani",
            "Dr Abha Verma",
            "Dr Chin Valsan",
            "Dr Beena Kurian",
        ),
        5: ("Dr Natasha Khushalani", "Dr Chin Valsan"),
    },
}

SYDNEY = ZoneInfo("Australia/Sydney")

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


def format_group_block() -> str:
    verify_note = (
        "Fields marked VERIFY are unknown. Do not invent them. "
        "Offer to check with the team, take a message, or transfer. "
        "Listed dentists are who works at that site, not who is rostered today."
    )
    blocks = [format_branch_block(branch) for branch in BRANCHES.values()]
    return "\n\n".join(blocks) + f"\n\n- {verify_note}"


def format_dialled_hint(branch: Branch) -> str:
    return (
        f"The caller may have dialled {branch.trading_name} in {branch.suburb}. "
        "That is a hint only — do not lock the call to this clinic. Ask where "
        "they are and route to the nearest (or preferred-dentist) site."
    )


def _as_date(value: date | str | None) -> date:
    if value is None:
        return datetime.now(SYDNEY).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _norm_place(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def rostered_clinicians(
    branch_id: str, on_date: date | str | int | None = None
) -> tuple[str, ...]:
    """Dentists mock-rostered at a branch on a calendar date or weekday int."""
    branch = get_branch(branch_id)
    weekday = on_date if isinstance(on_date, int) else _as_date(on_date).weekday()
    if weekday == 6:
        return ()
    return WEEKDAY_ROSTER.get(branch.id, {}).get(weekday, ())


def _clinic_summary(branch: Branch, *, on_date: date, nearest: bool) -> dict[str, Any]:
    return {
        "branch_id": branch.id,
        "trading_name": branch.trading_name,
        "suburb": branch.suburb,
        "address": branch.address,
        "phone": branch.phone,
        "hours": branch.hours,
        "nearest": nearest,
        "listed_dentists": list(branch.dentists),
        "rostered_today": list(rostered_clinicians(branch.id, on_date)),
    }


def suggest_clinics_for_location(
    location: str,
    *,
    on_date: date | str | None = None,
) -> dict[str, Any]:
    """Rank group clinics for a suburb. Unknown places must not be guessed."""
    day = _as_date(on_date)
    query = _norm_place(location)
    if not query:
        return {
            "ok": False,
            "reason": "missing_location",
            "note": "Ask which suburb they are in or near. Do not assume a clinic.",
            "clinics": [],
        }

    matched_suburb: str | None = None
    ranked: tuple[str, ...] | None = None
    for suburb in sorted(SUBURB_NEARBY, key=len, reverse=True):
        if suburb == query or suburb in query:
            matched_suburb = suburb
            ranked = SUBURB_NEARBY[suburb]
            break

    if ranked is None:
        clinics = [
            _clinic_summary(branch, on_date=day, nearest=False)
            for branch in BRANCHES.values()
        ]
        return {
            "ok": True,
            "matched": False,
            "query": location,
            "matched_suburb": None,
            "nearest_branch_id": None,
            "branch_ids": [branch.id for branch in BRANCHES.values()],
            "date": day.isoformat(),
            "clinics": clinics,
            "note": (
                "Suburb not in the Illawarra map. Do not guess. Ask which of "
                "Barrack Heights, Dapto, or Woonona is easier, or offer all three."
            ),
        }

    clinics = [
        _clinic_summary(get_branch(branch_id), on_date=day, nearest=index == 0)
        for index, branch_id in enumerate(ranked)
    ]
    return {
        "ok": True,
        "matched": True,
        "query": location,
        "matched_suburb": matched_suburb,
        "nearest_branch_id": ranked[0],
        "branch_ids": list(ranked),
        "date": day.isoformat(),
        "clinics": clinics,
    }


def resolve_clinician(name: str) -> dict[str, Any]:
    """Match a spoken dentist name to a canonical listed clinician."""
    raw = (name or "").strip()
    if not raw:
        return {"ok": False, "reason": "missing_name"}
    query = re.sub(r"^dr\.?\s+", "", _norm_place(raw))
    if not query:
        return {"ok": False, "reason": "missing_name"}
    seen: list[str] = []
    for branch in BRANCHES.values():
        for dentist in branch.dentists:
            if dentist not in seen:
                seen.append(dentist)

    matches: list[str] = []
    for canonical in seen:
        compact = re.sub(r"^dr\.?\s+", "", _norm_place(canonical))
        tokens = compact.split()
        q_tokens = query.split()
        if query == compact:
            matches.append(canonical)
            continue
        if len(query) > 2 and query in compact:
            matches.append(canonical)
            continue
        if len(q_tokens) == 1 and len(q_tokens[0]) > 2 and q_tokens[0] in tokens:
            matches.append(canonical)

    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return {"ok": True, "clinician": unique[0]}
    if not unique:
        return {"ok": False, "reason": "clinician_not_found"}
    return {"ok": False, "reason": "ambiguous", "candidates": unique}


def lookup_clinician(
    name: str,
    *,
    on_date: date | str | None = None,
    near_branch_id: str | None = None,
) -> dict[str, Any]:
    """Where a dentist is listed vs rostered today. Never invent a clinician."""
    day = _as_date(on_date)
    resolved = resolve_clinician(name)
    if not resolved.get("ok"):
        result = dict(resolved)
        result["note"] = (
            "Do not invent a dentist. Offer dentists rostered at the nearest "
            "clinic or check with the team."
        )
        return result

    clinician = str(resolved["clinician"])
    listed_at = [
        branch.id for branch in BRANCHES.values() if clinician in branch.dentists
    ]
    rostered_at = [
        branch.id
        for branch in BRANCHES.values()
        if clinician in rostered_clinicians(branch.id, day)
    ]
    near: str | None = None
    if near_branch_id and near_branch_id.strip().lower() in BRANCHES:
        near = near_branch_id.strip().lower()

    available_at_nearest = bool(near and near in rostered_at)
    offer: dict[str, Any] | None = None
    if near and not available_at_nearest and rostered_at:
        other_id = rostered_at[0]
        other = get_branch(other_id)
        nearest_branch = get_branch(near)
        offer = {
            "branch_id": other.id,
            "trading_name": other.trading_name,
            "suburb": other.suburb,
            "note": (
                f"{clinician} is not rostered at {nearest_branch.trading_name} "
                f"today, but is available at {other.trading_name} in "
                f"{other.suburb}."
            ),
        }

    return {
        "ok": True,
        "clinician": clinician,
        "date": day.isoformat(),
        "listed_at": listed_at,
        "rostered_at": rostered_at,
        "near_branch_id": near,
        "available_at_nearest": available_at_nearest,
        "offer_other_clinic": offer,
    }


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
    return (
        f"{VOICE_INSTRUCTIONS}\n\n"
        f"{BACKEND_INSTRUCTIONS.format(dialled_hint=format_dialled_hint(branch), group_block=format_group_block())}"
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
