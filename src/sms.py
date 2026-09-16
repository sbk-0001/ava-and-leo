"""Appointment confirmation texts.

Every text is from the practice, Illawarra Dentists, and then names the clinic
the appointment is at. Texts go only to an Australian mobile the caller has
confirmed, and only when a provider is configured:

    SMS_PROVIDER=telnyx
    TELNYX_API_KEY=...               # Telnyx portal > API keys
    SMS_FROM=+614xxxxxxxx            # an SMS-enabled Telnyx number, or an
                                     # approved alphanumeric sender id
    TELNYX_MESSAGING_PROFILE_ID=...  # optional

SMS_PROVIDER=memory keeps texts in memory (tests, demos). Anything else: no
texts, and Ava never mentions one.

Docs: https://developers.telnyx.com/api/messaging/send-message
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, Protocol
from urllib.request import Request, urlopen

from persona import GROUP_TRADING_NAME, get_branch
from practice import PLACEHOLDER_DENTISTS

logger = logging.getLogger("ava.sms")

TextKind = Literal["booked", "moved", "cancelled"]
TELNYX_URL = "https://api.telnyx.com/v2/messages"
_MOBILE_RE = re.compile(r"^04\d{8}$")


class SmsSender(Protocol):
    async def send(self, to: str, text: str) -> dict[str, Any]: ...


def au_mobile_e164(value: str | None) -> str | None:
    """+614xxxxxxxx for an Australian mobile, otherwise None."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    if not _MOBILE_RE.match(digits):
        return None
    return "+61" + digits[1:]


def _when(result: Mapping[str, Any]) -> str:
    raw_date = str(result.get("date") or "").strip()
    raw_time = str(result.get("time") or "").strip()
    parts: list[str] = []
    try:
        day = date.fromisoformat(raw_date[:10])
        parts.append(f"{day.strftime('%a')} {day.day} {day.strftime('%b')}")
    except ValueError:
        pass
    match = re.match(r"^(\d{1,2}):(\d{2})", raw_time)
    if match:
        hour, minute = int(match.group(1)), match.group(2)
        suffix = "am" if hour < 12 else "pm"
        hour = hour % 12 or 12
        parts.append(f"at {hour}:{minute}{suffix}")
    return " ".join(parts)


def _dentist(result: Mapping[str, Any]) -> str:
    name = str(result.get("clinician") or "").strip()
    if not name or name.lower() in PLACEHOLDER_DENTISTS:
        return ""
    return f" with {name}"


def booking_text(
    kind: TextKind,
    result: Mapping[str, Any],
    *,
    first_name: str | None = None,
) -> str:
    """The confirmation text for a booking that is already locked in."""
    branch = get_branch(str(result.get("branch_id") or ""))
    hello = f"Hi {first_name}," if first_name else "Hi,"
    street = branch.address.split(",")[0].strip()
    where = f"{branch.trading_name}, {street}, {branch.suburb}"
    when = _when(result)
    when_part = f" on {when}" if when else ""
    head = f"{GROUP_TRADING_NAME}: {hello}"
    if kind == "cancelled":
        return (
            f"{head} your appointment at {branch.trading_name}{when_part} is "
            f"cancelled. To book again call {branch.phone}."
        )
    if kind == "moved":
        lead = "your appointment has moved. You're now booked at"
    else:
        lead = "you're booked at"
    return (
        f"{head} {lead} {where}{_dentist(result)}{when_part}. "
        f"Need to change it? Call {branch.phone}. Please give 24 hours notice "
        "or a $50 fee applies."
    )


class MemorySms:
    """Keeps texts in memory: tests and demos."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> dict[str, Any]:
        self.sent.append((to, text))
        return {"ok": True, "id": f"mem_{len(self.sent)}"}


class TelnyxSms:
    def __init__(
        self,
        *,
        api_key: str,
        from_: str,
        profile_id: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.from_ = from_
        self.profile_id = profile_id
        self.timeout_s = timeout_s

    def _post(self, to: str, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {"from": self.from_, "to": to, "text": text}
        if self.profile_id:
            body["messaging_profile_id"] = self.profile_id
        request = Request(
            TELNYX_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read() or b"{}")
        return {"ok": True, "id": (payload.get("data") or {}).get("id")}

    async def send(self, to: str, text: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._post, to, text)
        except Exception as exc:  # the call must go on regardless
            detail = str(exc)
            read = getattr(exc, "read", None)
            if callable(read):
                with contextlib.suppress(Exception):
                    detail = f"{detail}: {read().decode(errors='replace')[:200]}"
            logger.warning("telnyx sms failed to=%s error=%s", to[-3:], detail)
            return {"ok": False, "error": detail}


def sms_sender_from_env(env: Mapping[str, str] | None = None) -> SmsSender | None:
    import os

    environ = env if env is not None else os.environ
    provider = str(environ.get("SMS_PROVIDER") or "").strip().lower()
    if provider == "memory":
        return MemorySms()
    if provider == "telnyx":
        key = str(environ.get("TELNYX_API_KEY") or "").strip()
        sender = str(environ.get("SMS_FROM") or "").strip()
        if not key or not sender:
            logger.warning(
                "SMS_PROVIDER=telnyx but TELNYX_API_KEY or SMS_FROM is empty"
            )
            return None
        profile = str(environ.get("TELNYX_MESSAGING_PROFILE_ID") or "").strip()
        return TelnyxSms(api_key=key, from_=sender, profile_id=profile or None)
    return None
