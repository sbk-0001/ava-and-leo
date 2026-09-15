"""BookingProvider: diary behind an interface, never fake slots in agent logic.

Thursday path: MemoryBookingProvider seeds a realistic week for all three
branches. Zavy360BookingProvider is stubbed behind the same interface —
the public Zavy 360 API is early-access only (see docs/zavy360.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from persona import get_branch
from practice import Booking, PracticeClient, get_shared_practice, practice_from_env

logger = logging.getLogger("booking")

SYDNEY = ZoneInfo("Australia/Sydney")
PROVIDER_TIMEOUT_S = 8.0
CANONICAL_SLOT_ID_RE = re.compile(r"^slot_[a-z0-9]+_\d{4}-\d{2}-\d{2}_")
_TIME_OF_DAY_RE = re.compile(
    r"\b(morning|mornings|morno|arvo|afternoon|afternoons|evening|evenings)\b"
)


class BookingProvider(Protocol):
    """Office-system adapter. Implementations must not invent diary slots."""

    name: str

    async def check_availability(
        self,
        *,
        branch: str,
        appointment_type: str,
        date_range: str,
        clinician: str | None = None,
        limit: int | None = 12,
    ) -> dict[str, Any]: ...

    async def book_appointment(
        self,
        *,
        branch: str,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]: ...

    async def reschedule_appointment(
        self,
        *,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]: ...

    async def cancel_appointment(self, *, booking_id: str) -> dict[str, Any]: ...

    async def lookup_patient(self, *, mobile: str) -> dict[str, Any]: ...

    async def take_message(
        self,
        *,
        branch: str,
        name: str,
        mobile: str,
        reason: str,
    ) -> dict[str, Any]: ...


_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b")
_NEXT_WEEKDAY_RE = re.compile(rf"\bnext\s+({_WEEKDAY_ALT})\b")


def _next_calendar_week(now: date) -> tuple[date, date]:
    """Monday of the next calendar week through that Sunday (Sydney)."""
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    start = now + timedelta(days=days_until_monday)
    return start, start + timedelta(days=6)


def _next_weekday_after(now: date, weekday: int) -> date:
    """That weekday strictly after today. If today is Tuesday, next Tuesday is +7."""
    delta = (weekday - now.weekday()) % 7
    if delta == 0:
        delta = 7
    return now + timedelta(days=delta)


def parse_date_range(
    date_range: str,
    *,
    today: date | None = None,
) -> tuple[str, str]:
    """Accept ISO dates, 'today', 'this week', 'next week', 'next tuesday'."""
    now = today or datetime.now(SYDNEY).date()
    raw = _TIME_OF_DAY_RE.sub("", (date_range or "").strip().lower())
    raw = re.sub(r"[^\w\s/-]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw or raw in {"today", "asap", "soon"}:
        return now.isoformat(), now.isoformat()
    if raw in {"this week", "week"}:
        end = now + timedelta(days=6)
        return now.isoformat(), end.isoformat()
    if raw in {"tomorrow"}:
        nxt = now + timedelta(days=1)
        return nxt.isoformat(), nxt.isoformat()
    if _NEXT_WEEK_RE.search(raw):
        start, end = _next_calendar_week(now)
        return start.isoformat(), end.isoformat()
    weekday_match = _NEXT_WEEKDAY_RE.search(raw)
    if weekday_match:
        day = _next_weekday_after(now, _WEEKDAYS[weekday_match.group(1)])
        return day.isoformat(), day.isoformat()
    if "/" in raw:
        start_s, end_s = raw.split("/", 1)
        return start_s.strip(), end_s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw, raw
    return now.isoformat(), (now + timedelta(days=6)).isoformat()


def is_canonical_slot_id(slot_id: str) -> bool:
    """Diary ids look like slot_<branch>_<YYYY-MM-DD>_<time>_dr-..."""
    return bool(CANONICAL_SLOT_ID_RE.match((slot_id or "").strip()))


def invalid_slot_id_result(slot_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "confirmed": False,
        "reason": "invalid_slot_id",
        "slot_id": slot_id,
        "note": (
            "That slot_id is not a diary id. Call check_availability again "
            "and book only an exact slot_id from the slots list. "
            "Never invent or reconstruct times or ids."
        ),
    }


def _norm_clinician(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip().lower()
    return re.sub(r"^dr\.?\s+", "", text)


def filter_slots_by_clinician(
    slots: list[dict[str, Any]],
    clinician: str | None,
) -> list[dict[str, Any]]:
    """Filter named diary slots. If none carry a clinician field, leave them as-is."""
    wanted = (clinician or "").strip()
    if not wanted:
        return list(slots)
    if not any(str(slot.get("clinician") or "").strip() for slot in slots):
        return list(slots)
    needle = _norm_clinician(wanted)
    return [
        slot
        for slot in slots
        if needle and needle in _norm_clinician(str(slot.get("clinician") or ""))
    ]


def spoken_two_slot_offer(slots: list[dict[str, Any]]) -> str:
    """Speech-ready offer of the two best diary slots. Never invents times."""
    best = list(slots)[:2]
    if not best:
        return "Yeah nah, nothing in that window — want me to try another day?"
    parts: list[str] = []
    for slot in best:
        try:
            day = date.fromisoformat(str(slot.get("date")))
            weekday = day.strftime("%A")
            day_n = day.day
            when = f"{weekday} the {day_n}"
        except (TypeError, ValueError):
            when = str(slot.get("date") or "that day")
        time_s = str(slot.get("time") or "").strip()
        if time_s:
            when = f"{when} at {time_s}"
        name = str(slot.get("clinician") or "").strip()
        if name:
            when = f"{when} with {name}"
        parts.append(when)
    if len(parts) == 1:
        return f"I've got {parts[0]} — that any good?"
    return f"I've got {parts[0]}, or {parts[1]} — which suits?"


def cancellation_fee_applies(
    appointment_date: str,
    appointment_time: str,
    *,
    now: datetime | None = None,
) -> bool:
    """$50 if inside 24 hours or a no-show. Never waived in code."""
    current = now or datetime.now(SYDNEY)
    try:
        hour, minute = (appointment_time or "00:00").split(":")[:2]
        when = datetime(
            *date.fromisoformat(appointment_date).timetuple()[:3],
            int(hour),
            int(minute),
            tzinfo=SYDNEY,
        )
    except (TypeError, ValueError):
        return True
    return when <= current + timedelta(hours=24)


class MemoryBookingProvider:
    """Seeded in-memory diary. Slots live here, not in the agent prompt."""

    name = "memory"

    def __init__(self, client: PracticeClient) -> None:
        self.client = client

    async def check_availability(
        self,
        *,
        branch: str,
        appointment_type: str,
        date_range: str,
        clinician: str | None = None,
        limit: int | None = 12,
    ) -> dict[str, Any]:
        if self.client.mode == "disconnected":
            return self.client._unavailable("check_availability")
        start, end = parse_date_range(date_range)
        diary = await self.client.list_diary(
            branch_id=get_branch(branch).id, date_from=start, date_to=end
        )
        open_slots = [slot for slot in diary.get("slots", []) if not slot.get("taken")]
        filtered = filter_slots_by_clinician(open_slots, clinician)
        if limit is not None:
            filtered = filtered[:limit]
        payload: dict[str, Any] = {
            "ok": True,
            "branch_id": get_branch(branch).id,
            "appointment_type": appointment_type,
            "date_from": start,
            "date_to": end,
            # Full list when limit is None (14-day cache prewarm). Agent-facing
            # calls keep the default trim of 12.
            "slots": filtered,
        }
        if clinician:
            payload["clinician"] = clinician
        if (
            clinician
            and not filtered
            and any(str(slot.get("clinician") or "").strip() for slot in open_slots)
        ):
            payload["note"] = (
                "No slots for that dentist in this range. Do not invent a time. "
                "Offer another dentist or another day."
            )
        return payload

    async def book_appointment(
        self,
        *,
        branch: str,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.book_appointment(
            branch_id=get_branch(branch).id,
            slot_id=slot_id,
            reason=reason,
            patient_id=patient_id,
            name=name,
            phone=mobile,
            date_of_birth=date_of_birth,
        )

    async def reschedule_appointment(
        self,
        *,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]:
        return await self.client.reschedule_appointment(
            booking_id=booking_id, new_slot_id=new_slot_id
        )

    async def cancel_appointment(self, *, booking_id: str) -> dict[str, Any]:
        booking = self.client.bookings.get(booking_id)
        result = await self.client.cancel_appointment(booking_id=booking_id)
        if not result.get("ok") or booking is None:
            return result
        fee = cancellation_fee_applies(booking.date, booking.time)
        result["fee_applies"] = fee
        result["fee_aud"] = 50 if fee else 0
        result["policy"] = (
            "$50 fee for a no-show or a cancellation inside 24 hours. "
            "Mention it warmly. Never waive it."
        )
        return result

    async def lookup_patient(self, *, mobile: str) -> dict[str, Any]:
        if self.client.mode == "disconnected":
            return self.client._unavailable("lookup_patient")
        digits = re.sub(r"\D", "", mobile)
        matches = []
        for patient in self.client.patients.values():
            if re.sub(r"\D", "", patient.phone) == digits or re.sub(
                r"\D", "", patient.phone
            ).endswith(digits[-9:]):
                matches.append(
                    {
                        "patient_id": patient.patient_id,
                        "name": patient.name,
                        "phone": patient.phone,
                        "date_of_birth": patient.date_of_birth,
                    }
                )
        bookings = []
        if matches:
            pid = matches[0]["patient_id"]
            for booking in self.client.bookings.values():
                if booking.cancelled or booking.patient_id != pid:
                    continue
                bookings.append(
                    {
                        "booking_id": booking.booking_id,
                        "slot_id": booking.slot_id,
                        "branch_id": booking.branch_id,
                        "date": booking.date,
                        "time": booking.time,
                        "clinician": booking.clinician,
                        "reason": booking.reason,
                    }
                )
        return {
            "ok": True,
            "patients": matches,
            "bookings": bookings,
            "is_existing_patient": bool(matches),
        }

    async def take_message(
        self,
        *,
        branch: str,
        name: str,
        mobile: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self.client.leave_message(
            branch_id=get_branch(branch).id,
            caller_name=name,
            phone=mobile,
            body=reason,
        )


class Zavy360BookingProvider:
    """Stub. Zavy 360 API is early-access; no public booking REST for Thursday.

    When ZAVY360_API_KEY and ZAVY360_API_URL are set, requests are attempted
    and failures return unavailable — never invented slots.
    """

    name = "zavy360"

    def __init__(
        self,
        *,
        api_url: str = "",
        api_key: str = "",
        timeout_s: float = PROVIDER_TIMEOUT_S,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def _unavailable(
        self, action: str, reason: str = "zavy360_unavailable"
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": reason,
            "action": action,
            "provider": self.name,
            "note": (
                "Zavy360 is not usable for this call. Do not invent times. "
                "Offer to take a message, transfer, or have the team call back."
            ),
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.configured:
            return self._unavailable(path)
        try:
            import httpx
        except ImportError:
            return self._unavailable(path, "zavy360_http_missing")
        url = f"{self.api_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
            if response.status_code >= 400:
                return self._unavailable(path, f"zavy360_http_{response.status_code}")
            data = response.json()
            return data if isinstance(data, dict) else {"ok": True, "data": data}
        except Exception:
            logger.exception("Zavy360 %s %s failed", method, path)
            return self._unavailable(path, "zavy360_error")

    async def check_availability(
        self,
        *,
        branch: str,
        appointment_type: str,
        date_range: str,
        clinician: str | None = None,
        limit: int | None = 12,
    ) -> dict[str, Any]:
        start, end = parse_date_range(date_range)
        params: dict[str, Any] = {
            "branch": get_branch(branch).id,
            "appointment_type": appointment_type,
            "date_from": start,
            "date_to": end,
        }
        if clinician:
            params["clinician"] = clinician
        result = await self._request(
            "GET",
            "/appointments/availability",
            params=params,
        )
        slots = result.get("slots")
        if limit is not None and isinstance(slots, list):
            result = dict(result)
            result["slots"] = slots[:limit]
        return result

    async def book_appointment(
        self,
        *,
        branch: str,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/appointments",
            json={
                "branch": get_branch(branch).id,
                "slot_id": slot_id,
                "reason": reason,
                "patient_id": patient_id,
                "name": name,
                "mobile": mobile,
                "date_of_birth": date_of_birth,
            },
        )

    async def reschedule_appointment(
        self,
        *,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/appointments/{booking_id}/reschedule",
            json={"new_slot_id": new_slot_id},
        )

    async def cancel_appointment(self, *, booking_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/appointments/{booking_id}/cancel")

    async def lookup_patient(self, *, mobile: str) -> dict[str, Any]:
        return await self._request("GET", "/patients", params={"mobile": mobile})

    async def take_message(
        self,
        *,
        branch: str,
        name: str,
        mobile: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/messages",
            json={
                "branch": get_branch(branch).id,
                "name": name,
                "mobile": mobile,
                "reason": reason,
            },
        )


class TimeoutBookingProvider:
    """Wraps a provider and enforces a timeout so Ava can recover in character."""

    def __init__(
        self,
        inner: BookingProvider,
        *,
        timeout_s: float = PROVIDER_TIMEOUT_S,
        force_timeout: bool = False,
    ) -> None:
        self.inner = inner
        self.timeout_s = timeout_s
        self.force_timeout = force_timeout
        self.name = getattr(inner, "name", "timeout")

    async def _call(self, action: str, factory: Any) -> dict[str, Any]:
        if self.force_timeout:
            return {
                "ok": False,
                "reason": "timeout",
                "action": action,
                "note": (
                    "The diary timed out. Stay in character. Do not invent a slot. "
                    "Offer to try again, take a message, or transfer."
                ),
            }
        try:
            return await asyncio.wait_for(factory(), timeout=self.timeout_s)
        except TimeoutError:
            logger.warning("booking provider timed out action=%s", action)
            return {
                "ok": False,
                "reason": "timeout",
                "action": action,
                "note": (
                    "The diary timed out. Stay in character. Do not invent a slot. "
                    "Offer to try again, take a message, or transfer."
                ),
            }

    async def check_availability(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "check_availability", lambda: self.inner.check_availability(**kwargs)
        )

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "book_appointment", lambda: self.inner.book_appointment(**kwargs)
        )

    async def reschedule_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "reschedule_appointment",
            lambda: self.inner.reschedule_appointment(**kwargs),
        )

    async def cancel_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "cancel_appointment", lambda: self.inner.cancel_appointment(**kwargs)
        )

    async def lookup_patient(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "lookup_patient", lambda: self.inner.lookup_patient(**kwargs)
        )

    async def take_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call(
            "take_message", lambda: self.inner.take_message(**kwargs)
        )


class ToolPacingProvider:
    """Force a slow or hanging diary lookup so the filler ladder can be heard.

    Wrap *outside* the availability cache so a cache hit still takes the
    requested time. Hang returns timeout after `lookup_hang_s` without slots.
    """

    def __init__(
        self,
        inner: BookingProvider,
        *,
        lookup_delay_s: float = 0.0,
        lookup_hang_s: float = 0.0,
    ) -> None:
        self.inner = inner
        self.lookup_delay_s = float(lookup_delay_s or 0.0)
        self.lookup_hang_s = float(lookup_hang_s or 0.0)
        self.name = getattr(inner, "name", "paced")

    async def check_availability(self, **kwargs: Any) -> dict[str, Any]:
        if self.lookup_hang_s > 0:
            await asyncio.sleep(self.lookup_hang_s)
            return {
                "ok": False,
                "reason": "timeout",
                "action": "check_availability",
                "note": (
                    "The diary did not come back. Stay in character. Do not invent "
                    "a slot. Offer to take a message or transfer."
                ),
            }
        if self.lookup_delay_s > 0:
            await asyncio.sleep(self.lookup_delay_s)
        return await self.inner.check_availability(**kwargs)

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.book_appointment(**kwargs)

    async def reschedule_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.reschedule_appointment(**kwargs)

    async def cancel_appointment(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.cancel_appointment(**kwargs)

    async def lookup_patient(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.lookup_patient(**kwargs)

    async def take_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self.inner.take_message(**kwargs)


def unwrap_practice_client(provider: Any) -> PracticeClient | None:
    current: Any = provider
    for _ in range(8):
        if current is None:
            return None
        client = getattr(current, "client", None)
        if isinstance(client, PracticeClient):
            return client
        current = getattr(current, "inner", None)
    return None


def seed_inside_24h_booking(
    client: PracticeClient | None,
    *,
    now: datetime | None = None,
) -> Booking | None:
    """Seed Priya Nair with a booking that triggers the $50 cancel fee."""
    if client is None:
        return None
    current = now or datetime.now(SYDNEY)
    when = current + timedelta(hours=12)
    client.seed_patient(
        patient_id="pat_priya_ns",
        name="Priya Nair",
        phone="0413000222",
    )
    slot_id = "slot_priya_ns_24h"
    client.seed_slot(
        slot_id=slot_id,
        branch_id="shellharbour",
        date=when.date().isoformat(),
        time=when.strftime("%H:%M"),
        clinician="Dr Mohit Tolani",
        taken=True,
    )
    booking = Booking(
        booking_id="bkg_priya_ns",
        slot_id=slot_id,
        branch_id="shellharbour",
        patient_id="pat_priya_ns",
        date=when.date().isoformat(),
        time=when.strftime("%H:%M"),
        clinician="Dr Mohit Tolani",
        reason="check-up",
    )
    client.bookings[booking.booking_id] = booking
    client.save()
    return booking


def apply_job_booking_overrides(
    booking: BookingProvider,
    metadata: Mapping[str, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> BookingProvider:
    """Per-call delay / hang / 24h cancel seed from agent job metadata."""
    del env
    meta = metadata or {}
    if meta.get("seed_cancel_24h"):
        seed_inside_24h_booking(unwrap_practice_client(booking))
    delay = float(meta.get("booking_delay_s") or 0)
    hang = float(meta.get("booking_hang_s") or 0)
    if delay > 0 or hang > 0:
        return ToolPacingProvider(booking, lookup_delay_s=delay, lookup_hang_s=hang)
    return booking


def booking_provider_from_env(
    *,
    is_telephony: bool = False,
    env: Mapping[str, str] | None = None,
    persist: bool = True,
) -> BookingProvider:
    environ = env if env is not None else os.environ
    kind = (
        str(environ.get("BOOKING_PROVIDER") or environ.get("PRACTICE_SOFTWARE") or "")
        .strip()
        .lower()
    )
    force_timeout = str(environ.get("BOOKING_FORCE_TIMEOUT", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    timeout_s = float(
        environ.get("BOOKING_TIMEOUT_S", PROVIDER_TIMEOUT_S) or PROVIDER_TIMEOUT_S
    )

    if kind in {"zavy360", "zavy"}:
        inner: BookingProvider = Zavy360BookingProvider(
            api_url=str(environ.get("ZAVY360_API_URL", "")).strip(),
            api_key=str(environ.get("ZAVY360_API_KEY", "")).strip(),
            timeout_s=timeout_s,
        )
    else:
        client = practice_from_env(
            is_telephony=is_telephony, persist=persist, env=environ
        )
        inner = MemoryBookingProvider(client)

    return TimeoutBookingProvider(
        inner, timeout_s=timeout_s, force_timeout=force_timeout
    )


def get_shared_booking_provider(
    *,
    is_telephony: bool = False,
    env: Mapping[str, str] | None = None,
) -> BookingProvider:
    """Share the mock diary singleton with the portal when using memory mode."""
    environ = env if env is not None else os.environ
    kind = (
        str(environ.get("BOOKING_PROVIDER") or environ.get("PRACTICE_SOFTWARE") or "")
        .strip()
        .lower()
    )
    if kind in {"zavy360", "zavy"}:
        return booking_provider_from_env(is_telephony=is_telephony, env=environ)
    client = get_shared_practice(is_telephony=is_telephony, env=environ)
    force_timeout = str(environ.get("BOOKING_FORCE_TIMEOUT", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    timeout_s = float(
        environ.get("BOOKING_TIMEOUT_S", PROVIDER_TIMEOUT_S) or PROVIDER_TIMEOUT_S
    )
    return TimeoutBookingProvider(
        MemoryBookingProvider(client),
        timeout_s=timeout_s,
        force_timeout=force_timeout,
    )
