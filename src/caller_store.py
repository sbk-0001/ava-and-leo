"""ByteVoice caller store keyed on E.164. Separate from PMS.

Minimum fields only. Retention is 12 months from last contact. Use the
record to AVOID ASKING, never to volunteer clinical or appointment detail
until DOB is verified.

See docs/privacy-caller-store.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sip_utils import normalize_au_phone

logger = logging.getLogger("ava.caller_store")

SYDNEY_UTC = timezone.utc
DEFAULT_PATH = Path(".data/caller_store.json")
RETENTION = timedelta(days=365)

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def to_e164(value: str | None) -> str:
    return normalize_au_phone(value)


def first_name(name: str | None) -> str | None:
    if not name:
        return None
    token = name.strip().split()[0] if name.strip() else ""
    return token or None


def iso_now(now: datetime | None = None) -> str:
    stamp = now or datetime.now(SYDNEY_UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SYDNEY_UTC)
    return stamp.astimezone(SYDNEY_UTC).isoformat()


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SYDNEY_UTC)
    return stamp.astimezone(SYDNEY_UTC)


@dataclass
class CallerRecord:
    e164: str
    name: str | None = None
    date_of_birth: str | None = None
    preferred_branch: str | None = None
    dentist_preference: str | None = None
    booking_history: list[dict[str, Any]] = field(default_factory=list)
    last_contacted: str | None = None
    mobile: str | None = None

    def first_name(self) -> str | None:
        return first_name(self.name)


class CallerStore:
    """JSON-backed store. Tests may use an in-memory path or this class directly."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: dict[str, CallerRecord] = {}
        if path is not None:
            self.load()

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("caller store load failed")
            return
        items = raw.get("records") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict) or not item.get("e164"):
                continue
            record = CallerRecord(
                e164=str(item["e164"]),
                name=item.get("name"),
                date_of_birth=item.get("date_of_birth"),
                preferred_branch=item.get("preferred_branch"),
                dentist_preference=item.get("dentist_preference"),
                booking_history=list(item.get("booking_history") or []),
                last_contacted=item.get("last_contacted"),
                mobile=item.get("mobile"),
            )
            self.records[record.e164] = record

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [asdict(record) for record in self.records.values()]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def lookup(self, e164: str | None) -> CallerRecord | None:
        key = to_e164(e164)
        if not key:
            return None
        return self.records.get(key)

    def touch(
        self,
        e164: str,
        *,
        name: str | None = None,
        date_of_birth: str | None = None,
        preferred_branch: str | None = None,
        dentist_preference: str | None = None,
        booking: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CallerRecord:
        key = to_e164(e164)
        record = self.records.get(key) or CallerRecord(e164=key, mobile=key)
        if name:
            record.name = name
        if date_of_birth:
            record.date_of_birth = date_of_birth
        if preferred_branch:
            record.preferred_branch = preferred_branch
        if dentist_preference:
            record.dentist_preference = dentist_preference
        record.last_contacted = iso_now(now)
        record.mobile = key
        if booking:
            history = dict(booking)
            history.setdefault("at", record.last_contacted)
            record.booking_history.append(history)
            record.booking_history = record.booking_history[-20:]
        self.records[key] = record
        self.save()
        return record

    def open_bookings(self, e164: str | None) -> list[dict[str, Any]]:
        record = self.lookup(e164)
        if record is None:
            return []
        open_rows: list[dict[str, Any]] = []
        for item in record.booking_history:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status in {"cancelled", "canceled", "completed", "no_show"}:
                continue
            if not item.get("booking_id"):
                continue
            open_rows.append(dict(item))
        return open_rows

    def mark_cancelled(self, e164: str | None, booking_id: str) -> None:
        record = self.lookup(e164)
        if record is None or not booking_id:
            return
        for item in record.booking_history:
            if str(item.get("booking_id") or "") == booking_id:
                item["status"] = "cancelled"
        self.save()

    def purge_older_than(
        self,
        *,
        now: datetime | None = None,
        retention: timedelta = RETENTION,
    ) -> list[str]:
        current = now or datetime.now(SYDNEY_UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SYDNEY_UTC)
        cutoff = current - retention
        removed: list[str] = []
        for key, record in list(self.records.items()):
            stamp = parse_iso(record.last_contacted)
            if stamp is None or stamp < cutoff:
                removed.append(key)
                del self.records[key]
        if removed:
            self.save()
        logger.info(
            "caller store purged %s records older than %s", len(removed), cutoff.date()
        )
        return removed


_SHARED: CallerStore | None = None


def caller_store_from_env(env: Mapping[str, str] | None = None) -> CallerStore:
    environ = env if env is not None else os.environ
    raw = str(environ.get("CALLER_STORE_PATH") or "").strip()
    path = Path(raw) if raw else DEFAULT_PATH
    return CallerStore(path)


def get_shared_caller_store(env: Mapping[str, str] | None = None) -> CallerStore:
    global _SHARED
    if _SHARED is None:
        _SHARED = caller_store_from_env(env)
    return _SHARED


def reset_shared_caller_store() -> None:
    global _SHARED
    _SHARED = None


def apply_record_to_state(state: Any, record: CallerRecord) -> None:
    """Populate CallState for greet-by-name / skip-number. Clinical fields stay private."""
    state.known_caller = True
    state.ani = record.e164
    if getattr(state, "name_corrected", False):
        pass
    elif record.name and not state.caller_name:
        state.caller_name = record.name
    digits = re.sub(r"\D", "", record.e164)
    if digits.startswith("61") and len(digits) == 11:
        national = "0" + digits[2:]
    else:
        national = record.mobile or state.caller_mobile
    if national and not state.caller_mobile:
        state.caller_mobile = national
        state.mobile_confirmed = True  # confirmed on an earlier call
    if record.preferred_branch and not getattr(state, "preferred_branch", None):
        state.preferred_branch = record.preferred_branch
    state.usual_dentist = record.dentist_preference
    if record.date_of_birth:
        # Seed the record we verify against, so a caller who already gave us a
        # date of birth is not asked for it on every future call.
        existing = state.pms_record if isinstance(state.pms_record, dict) else {}
        patients = list(existing.get("patients") or [])
        if not patients:
            patients = [{"name": record.name, "date_of_birth": record.date_of_birth}]
        elif isinstance(patients[0], dict) and not patients[0].get("date_of_birth"):
            patients[0]["date_of_birth"] = record.date_of_birth
        existing["patients"] = patients
        state.pms_record = existing
    last = record.booking_history[-1] if record.booking_history else None
    if last:
        state.last_appointment_private = last


def upsert_from_booking(
    store: CallerStore, state: Any, result: Mapping[str, Any]
) -> CallerRecord | None:
    mobile = getattr(state, "caller_mobile", None) or result.get("mobile")
    key = to_e164(str(mobile or getattr(state, "ani", None) or ""))
    if not key:
        return None
    booking = None
    if result.get("ok") and result.get("confirmed"):
        booking = {
            "booking_id": result.get("booking_id"),
            "slot_id": result.get("slot_id") or getattr(state, "confirmed_slot", None),
            "branch": getattr(state, "branch", None),
            "clinician": result.get("clinician"),
        }
    return store.touch(
        key,
        name=getattr(state, "caller_name", None),
        preferred_branch=getattr(state, "branch", None),
        dentist_preference=getattr(state, "preferred_clinician", None),
        booking=booking,
    )
